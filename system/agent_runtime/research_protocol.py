"""Deterministic protocols for the three long-horizon research tasks.

The targets are completion guards, not prompts to collect arbitrary papers.
Coverage and saturation still matter; a count alone never marks a task done.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


RESEARCH_PROTOCOLS: dict[str, dict[str, Any]] = {
    "agentic_rl": {
        "title": "Agentic RL research-direction study",
        "target_papers": 30,
        "topic_minimum": 2,
        "required_topics": {
            "training_systems": ["asynchronous", "rollout", "training system", "staleness", "异步", "训练系统"],
            "reward_and_credit": ["reward", "credit assignment", "advantage", "奖励", "信用分配"],
            "search_and_tools": ["search agent", "tool use", "retrieval", "搜索", "工具"],
            "long_horizon": ["multi-turn", "long-horizon", "memory", "多轮", "长时程", "记忆"],
            "safety_and_evaluation": ["safety", "evaluation", "verifier", "benchmark", "安全", "评测", "验证器"],
        },
        "budget": {"max_cycles": 8, "max_web_calls": 2, "max_discovery_calls": 12, "max_imports": 30},
        "deliverable": "research_direction_brief",
    },
    "kv_cache": {
        "title": "KV cache technology landscape",
        "target_papers": 24,
        "topic_minimum": 3,
        "required_topics": {
            "structured_compression": ["compression", "merging", "cross-layer", "sharing", "压缩", "合并", "跨层"],
            "eviction": ["eviction", "pruning", "sparse", "淘汰", "剪枝", "稀疏"],
            "quantization": ["quantization", "low-bit", "int4", "int2", "量化", "低比特"],
            "offloading": ["offload", "swap", "migration", "cpu", "nvme", "卸载", "迁移", "换入"],
            "scheduling": ["scheduling", "serving", "pagedattention", "memory management", "调度", "服务", "内存管理"],
        },
        "budget": {"max_cycles": 8, "max_web_calls": 2, "max_discovery_calls": 12, "max_imports": 30},
        "deliverable": "technology_landscape",
    },
    "agent_harness": {
        "title": "Agent harness interview guide",
        "target_papers": 15,
        "topic_minimum": 2,
        "required_topics": {
            "loop_and_planning": ["agent loop", "termination", "planning", "workflow", "循环", "终止", "规划"],
            "tools": ["tool use", "function calling", "sandbox", "工具", "沙箱"],
            "context_and_memory": ["context", "memory", "compaction", "上下文", "记忆", "压缩"],
            "runtime_and_recovery": ["runtime", "checkpoint", "recovery", "retry", "恢复", "重试", "运行时"],
            "trace_eval_safety": ["trace", "evaluation", "safety", "observability", "评测", "安全", "可观测"],
        },
        "budget": {"max_cycles": 8, "max_web_calls": 3, "max_discovery_calls": 10, "max_imports": 20},
        "deliverable": "interview_guide",
    },
}


def get_research_protocol(task_key: str) -> dict[str, Any]:
    try:
        return deepcopy(RESEARCH_PROTOCOLS[str(task_key)])
    except KeyError as exc:
        raise ValueError(f"Unknown research task: {task_key}") from exc
