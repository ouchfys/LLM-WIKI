"""Token-based prompt construction. Raw records are never modified here."""
import json
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Optional

SUMMARY_ROLE = "[SYSTEM_CONTEXT_SUMMARY]"
TRIM_MARK = "\n[内容因上下文预算省略；原文仍保留]"


_pressure_handler = ContextVar("context_pressure_handler", default=None)


class ContextBudgetExceeded(ValueError):
    pass


class TokenCounter:
    def __init__(self, tokenizer_path: str = ""):
        self.tokenizer = None
        self.encoding = None
        if tokenizer_path:
            from tokenizers import Tokenizer
            self.tokenizer = Tokenizer.from_file(tokenizer_path)
            self.mode = "configured_tokenizer"
        else:
            try:
                import tiktoken
                self.encoding = tiktoken.get_encoding("cl100k_base")
                self.mode = "cl100k_estimate_1.25x"
            except Exception:
                self.mode = "utf8_byte_upper_estimate"

    def count(self, text: str) -> int:
        text = str(text or "")
        if self.tokenizer:
            return len(self.tokenizer.encode(text, add_special_tokens=False).ids)
        if self.encoding:
            return math.ceil(len(self.encoding.encode(text, disallowed_special=())) * 1.25)
        return len(text.encode("utf-8"))

    def clip(self, text: str, budget: int) -> str:
        text = str(text or "")
        budget = max(0, int(budget))
        if self.count(text) <= budget:
            return text
        if self.count(TRIM_MARK) > budget:
            return ""
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.count(text[:mid] + TRIM_MARK) <= budget:
                low = mid
            else:
                high = mid - 1
        result = text[:low] + TRIM_MARK
        while self.count(result) > budget and low:
            low -= 1
            result = text[:low] + TRIM_MARK
        return result


@dataclass(frozen=True)
class ContextPolicy:
    window: int = 1_000_000
    output_reserve: int = 4096
    safety: int = 2048
    history: int = 993856
    planning_history: int = 993856
    summary: int = 8192
    compact_trigger: int = 800_000
    compact_keep: int = 160_000
    auto_compact: bool = True

    def __post_init__(self):
        if self.output_reserve < 1200 or self.safety < 0:
            raise ValueError("Reserve at least 1200 output tokens and a nonnegative safety margin")
        if self.window - self.output_reserve - self.safety < 2048:
            raise ValueError("Context window must leave at least 2048 input tokens")
        if min(self.history, self.planning_history, self.summary, self.compact_keep) <= 0:
            raise ValueError("Context section budgets must be positive")
        if self.compact_trigger <= self.compact_keep:
            raise ValueError("Compaction trigger must exceed retention budget")

    @property
    def input_limit(self):
        return self.window - self.output_reserve - self.safety

    @classmethod
    def from_env(cls, model=None):
        from system.core.config import DEEPSEEK_CHAT_MODEL
        model = model or DEEPSEEK_CHAT_MODEL
        known = {"deepseek-v4-flash": 1_000_000, "deepseek-v4-pro": 1_000_000}
        configured = os.getenv("PAPERWIKI_CONTEXT_WINDOW")
        if not configured and model not in known:
            raise ValueError(f"Configure PAPERWIKI_CONTEXT_WINDOW for unknown model {model!r}")
        window = int(configured) if configured else known[model]
        if model in known and window > known[model]:
            raise ValueError("Configured window exceeds the verified model context length")
        runtime = int(os.getenv("PAPERWIKI_CONTEXT_RUNTIME_LIMIT", window))
        window = min(window, runtime)
        output = int(os.getenv("PAPERWIKI_CONTEXT_OUTPUT_RESERVE", 4096))
        safety = int(os.getenv("PAPERWIKI_CONTEXT_SAFETY", 2048))
        available = window - output - safety
        values = dict(window=window, output_reserve=output, safety=safety,
                      history=available, planning_history=available,
                      summary=min(8192, max(256, available // 20)),
                      compact_trigger=int(window * 0.8), compact_keep=int(window * 0.16))
        for name in ("summary", "compact_trigger", "compact_keep"):
            values[name] = int(os.getenv("PAPERWIKI_CONTEXT_" + name.upper(), values[name]))
        values["auto_compact"] = os.getenv("PAPERWIKI_CONTEXT_AUTO_COMPACT", "true").lower() not in {"0", "false", "no"}
        return cls(**values)


@lru_cache(maxsize=1)
def default_counter():
    return TokenCounter(os.getenv("PAPERWIKI_TOKENIZER_PATH", ""))


class ContextBudget:
    def __init__(self, policy: Optional[ContextPolicy] = None, counter=None, model=None):
        self.policy = policy or ContextPolicy.from_env(model)
        self.counter = counter or default_counter()

    @contextmanager
    def on_pressure(self, handler):
        token = _pressure_handler.set(handler)
        try:
            yield
        finally:
            _pressure_handler.reset(token)

    def recent(self, history, budget=None):
        """A contiguous newest suffix; oversized latest turn gets explicit clipping."""
        budget = self.policy.history if budget is None else max(0, budget)
        turns = [tuple(item[:2]) for item in history or []
                 if isinstance(item, (tuple, list)) and len(item) >= 2 and item[0] != SUMMARY_ROLE]
        selected = []
        for q, a in reversed(turns):
            cost = self.counter.count(f"User: {q}\nAssistant: {a}\n") + 8
            if cost > budget:
                if not selected and budget > 64:
                    q_budget = min(self.counter.count(str(q)), (budget - 32) // 2)
                    q = self.counter.clip(str(q), q_budget)
                    a = self.counter.clip(str(a), budget - self.counter.count(q) - 32)
                    selected.append((q, a))
                break
            selected.append((q, a))
            budget -= cost
        return list(reversed(selected))

    def history_text(self, history, budget=None):
        return "\n".join(f"User: {q}\nAssistant: {a}" for q, a in self.recent(history, budget))

    def summary_text(self, history):
        text = next((str(a) for q, a in history if q == SUMMARY_ROLE), "")
        return self.counter.clip(text, self.policy.summary)

    def prune_tool_text(self, text, cap):
        """Keep newest complete observation blocks first; original observations are persisted."""
        if self.counter.count(text) <= cap:
            return text
        blocks = text.split("\n[Observation ")
        blocks = [blocks[0]] + ["[Observation " + block for block in blocks[1:]]
        marker = "[较早工具观察因上下文压力省略；可按 result_id 读取原始结果]\n"
        remaining = max(0, cap - self.counter.count(marker))
        kept = []
        for block in reversed(blocks):
            cost = self.counter.count(block + "\n")
            if cost > remaining:
                if not kept:
                    kept.append(self.counter.clip(block, remaining))
                break
            kept.append(block)
            remaining -= cost
        return self.counter.clip(marker + "\n".join(reversed(kept)), cap)

    def compose(self, required: str, sections, *, extra_tokens=0):
        """Sections are (label, text, cap), in priority order. Never cut required input."""
        available = self.policy.input_limit - extra_tokens - 64
        if self.counter.count(required) > available:
            raise ContextBudgetExceeded("当前问题与系统规则超过输入预算，请缩短问题或将长资料导入知识库。")
        # Measure the complete candidate after per-source bounds, before dropping history.
        # Callables are re-evaluated after a checkpoint changes the shared history snapshot.
        handler = _pressure_handler.get()
        if handler:
            candidate = required + "".join(
                "\n\n" + label + ":\n" + str(value(cap) if callable(value) else self.counter.clip(str(value), cap))
                for label, value, cap in sections if value)
            request_tokens = self.counter.count(candidate) + extra_tokens + 128
            if request_tokens >= min(self.policy.compact_trigger, self.policy.input_limit):
                # Tool observations are derived views; original results remain in SQLite.
                sections = [(label, lambda n, text=value: self.prune_tool_text(str(text), n), min(cap, max(512, self.policy.input_limit // 10)))
                            if label in {"Previous observations", "Tool Observations"} else (label, value, cap)
                            for label, value, cap in sections]
                candidate = required + "".join(
                    "\n\n" + label + ":\n" + str(value(cap) if callable(value) else self.counter.clip(str(value), cap))
                    for label, value, cap in sections if value)
                request_tokens = self.counter.count(candidate) + extra_tokens + 128
            handler(request_tokens)
        result = required
        for label, text, cap in sections:
            if not text:
                continue
            prefix = f"\n\n{label}：\n"
            remaining = available - self.counter.count(result + prefix) - 16
            if remaining < 32:
                continue
            section_budget = min(cap, remaining)
            value = text(section_budget) if callable(text) else text
            part = self.counter.clip(str(value), section_budget)
            candidate = result + prefix + part
            if part and self.counter.count(candidate) <= available:
                result = candidate
        return result

    def check_request(self, payload, *, output_tokens, tools=None):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        cost = self.counter.count(text) + 64
        if tools:
            cost += self.counter.count(json.dumps(tools, ensure_ascii=False)) + 64
        if cost > self.policy.input_limit or cost + output_tokens + self.policy.safety > self.policy.window:
            raise ContextBudgetExceeded("模型请求超过配置的上下文预算；请缩短当前问题或增大已确认的模型窗口配置。")
        from system.agent_runtime.control import get_run_control
        control = get_run_control()
        if control is not None and hasattr(control, "context_usage"):
            control.context_usage.update(input_tokens_estimate=cost, output_reserve=self.policy.output_reserve,
                window=self.policy.window, input_limit=self.policy.input_limit, counter=self.counter.mode)
        return cost
