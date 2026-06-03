"""
用户偏好抽取器

每轮对话结束后，通过 LLM 抽取用户的稳定偏好和情节记忆。
核心设计：
- 区分"本轮指令"和"持久偏好"，只写入高置信度的偏好
- key 使用枚举，不允许自由文本 key
- 情节记忆（已讲过的论文/话题）单独管理，带过期
"""

import json
import re
from typing import Any, Dict, List, Optional


# ---- 偏好 key 枚举 ----
# 只有在这个集合里的 key 才会被写入 user_profile
ALLOWED_PROFILE_KEYS = {
    "language_preference",      # 用户偏好语言：中文 / 英文
    "answer_length",            # 答案长度：short / medium / long
    "answer_style",             # 答案风格：conclusion_first / evidence_first / balanced
    "retrieval_strategy",       # 常用检索策略：main / main_rerank
    "detail_level",             # 细节程度：high / medium / low
    "citation_preference",      # 引用偏好：inline / footnote / none
    "explanation_style",        # 解释风格：technical / simplified / analogy
}

EXTRACTION_PROMPT_TEMPLATE = """\
你是一个用户偏好分析器。根据以下对话，判断用户是否透露了任何**稳定的个人偏好**。

## 严格区分规则
- **稳定偏好**：用户表达了一种长期适用的习惯或喜好（例如"我一般看中文的"、"以后回答简短点"）
- **本轮指令**：仅针对当前问题的临时要求（例如"这次给我详细说说"、"帮我翻译成英文"）
- 只提取**稳定偏好**，忽略本轮指令

## 可识别的偏好类型（key 枚举）
- language_preference: 用户偏好的回答语言 (中文 / 英文)
- answer_length: 用户偏好的答案长度 (short / medium / long)
- answer_style: 用户偏好的答案组织方式 (conclusion_first / evidence_first / balanced)
- retrieval_strategy: 用户偏好的检索策略 (main / main_rerank)
- detail_level: 用户偏好的细节程度 (high / medium / low)
- citation_preference: 用户偏好的引用方式 (inline / footnote / none)
- explanation_style: 用户偏好的解释风格 (technical / simplified / analogy)

## 对话内容
User: {user_query}
Assistant: {assistant_response}

## 输出格式
只输出 JSON，不要任何其他文本。
如果没有发现稳定偏好，输出空数组：
{{"preferences": []}}

如果发现了偏好，输出：
{{"preferences": [
  {{"key": "偏好 key", "value": "偏好值", "confidence": "high 或 low", "evidence": "用户原话片段"}}
]}}

注意：
- confidence 为 low 的不会被采纳，所以只有你很确定是稳定偏好时才标 high
- key 必须是上面列出的枚举值之一
- value 必须是对应 key 的合法取值之一
"""

EPISODIC_EXTRACTION_PROMPT_TEMPLATE = """\
你是一个对话分析器。根据以下对话，判断是否有**已经讲解/讨论过的论文或话题**需要记录。

## 记录范围
- 已经详细解释过的论文名称或话题
- 用户已经知道的概念（不需要再次解释）
- 仅记录本轮确实深入讨论过的内容，不要记录仅提及的内容

## 对话内容
User: {user_query}
Assistant: {assistant_response}

## 输出格式
只输出 JSON，不要任何其他文本。
如果没有需要记录的情节，输出空数组：
{{"episodes": []}}

如果有，输出：
{{"episodes": [
  {{"topic": "话题简述", "detail": "具体讲了什么", "paper": "相关论文名（如有）"}}
]}}
"""


class PreferenceExtractor:
    """从对话中抽取用户偏好和情节记忆。"""

    def __init__(self, llm):
        """
        Args:
            llm: SiliconFlowChat 实例，需要有 invoke(prompt) 方法
        """
        self.llm = llm

    def extract_preferences(
        self,
        user_query: str,
        assistant_response: str,
    ) -> List[Dict[str, str]]:
        """
        从单轮对话中抽取稳定偏好。

        Returns:
            只返回 confidence=high 且 key 在枚举范围内的偏好列表。
            每个元素: {"key": str, "value": str, "evidence": str}
        """
        prompt = EXTRACTION_PROMPT_TEMPLATE.format(
            user_query=user_query,
            assistant_response=self._truncate(assistant_response, 500),
        )

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[PreferenceExtractor] LLM 调用失败: {exc}")
            return []

        parsed = self._parse_json(raw)
        if not parsed or "preferences" not in parsed:
            return []

        results = []
        for item in parsed["preferences"]:
            key = str(item.get("key", "")).strip()
            value = str(item.get("value", "")).strip()
            confidence = str(item.get("confidence", "")).strip().lower()
            evidence = str(item.get("evidence", "")).strip()

            # 三重过滤：key 在枚举内 + confidence=high + value 非空
            if key not in ALLOWED_PROFILE_KEYS:
                print(f"[PreferenceExtractor] 忽略非法 key: {key}")
                continue
            if confidence != "high":
                print(f"[PreferenceExtractor] 忽略低置信度偏好: {key}={value} (confidence={confidence})")
                continue
            if not value:
                continue

            results.append({
                "key": key,
                "value": value,
                "evidence": evidence,
            })

        if results:
            print(f"[PreferenceExtractor] 提取到 {len(results)} 条偏好: "
                  f"{', '.join(f'{r['key']}={r['value']}' for r in results)}")

        return results

    def extract_episodes(
        self,
        user_query: str,
        assistant_response: str,
    ) -> List[Dict[str, str]]:
        """
        从单轮对话中抽取情节记忆（已讲过的论文/话题）。

        Returns:
            列表，每个元素: {"topic": str, "detail": str, "paper": str}
        """
        prompt = EPISODIC_EXTRACTION_PROMPT_TEMPLATE.format(
            user_query=user_query,
            assistant_response=self._truncate(assistant_response, 500),
        )

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[PreferenceExtractor] 情节抽取 LLM 调用失败: {exc}")
            return []

        parsed = self._parse_json(raw)
        if not parsed or "episodes" not in parsed:
            return []

        results = []
        for item in parsed["episodes"]:
            topic = str(item.get("topic", "")).strip()
            if not topic:
                continue
            results.append({
                "topic": topic,
                "detail": str(item.get("detail", "")).strip(),
                "paper": str(item.get("paper", "")).strip(),
            })

        if results:
            print(f"[PreferenceExtractor] 提取到 {len(results)} 条情节记忆: "
                  f"{', '.join(r['topic'] for r in results)}")

        return results

    @staticmethod
    def _truncate(text: str, limit: int = 500) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 返回中解析 JSON，容忍 markdown 代码块包裹。"""
        text = str(text or "").strip()

        # 去掉 markdown 代码块
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()

        # 提取第一个 JSON 对象
        start = text.find("{")
        if start == -1:
            return None

        candidate = text[start:]
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, dict) else None
