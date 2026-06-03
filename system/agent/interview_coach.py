"""
Interview Coach — generates interview questions and evaluates answers.

Uses Wiki cards for topic grounding and GraphRAG evidence for reference answers.
"""

import json
import re
from typing import Any, Dict, List, Optional


GENERATE_QUESTION_PROMPT = """\
You are an expert technical interviewer preparing questions for a candidate.

Candidate's learning goal: {user_goal}

Available reference topics from their knowledge base:
{wiki_topics}

Generate ONE interview question. Make it specific, answerable, and technically meaningful.

Topic: {topic}
Difficulty: {difficulty}
Question type: {question_type}

Return JSON only:
{{
  "question": "the interview question text",
  "topic": "topic area",
  "key_points": ["point 1", "point 2", "point 3"],
  "ideal_answer_brief": "a concise reference answer (2-3 sentences)"
}}
"""

EVALUATE_ANSWER_PROMPT = """\
You are an expert technical interviewer evaluating a candidate's answer.

Question: {question}
Candidate's Answer: {user_answer}

Reference key points that should be covered:
{key_points}

Reference answer: {ideal_answer}

Evaluate the candidate's answer on:
1. Accuracy — are technical claims correct?
2. Completeness — are key concepts covered?
3. Clarity — is the explanation well-structured?
4. Depth — is there evidence of deep understanding?

Return JSON only:
{{
  "score": <0-10>,
  "strengths": ["what was good"],
  "weaknesses": ["what needs improvement"],
  "missing_concepts": ["concepts the candidate should have mentioned"],
  "better_answer": "an improved model answer (3-5 sentences)",
  "follow_up_question": "a follow-up question to probe deeper"
}}
"""

GENERATE_FOLLOW_UP_PROMPT = """\
Based on the candidate's weak points below, generate ONE follow-up question:

Weak points: {weak_points}
Previous question: {previous_question}
Topic: {topic}

Return JSON only:
{{"question": "follow-up question text"}}
"""


class InterviewCoach:
    """Generates interview questions and evaluates user answers."""

    def __init__(
        self,
        llm,
        wiki_store=None,
        learning_profile=None,
        rag_pipeline=None,
    ):
        self.llm = llm
        self.wiki_store = wiki_store
        self.learning_profile = learning_profile
        self.rag_pipeline = rag_pipeline

    def generate_question(
        self,
        topic: str = None,
        difficulty: str = "medium",
        question_type: str = "conceptual",
    ) -> Dict[str, Any]:
        """Generate an interview question grounded in the user's wiki knowledge."""
        # Collect available wiki topics
        wiki_topics = "None"
        user_goal = ""
        if self.wiki_store:
            cards = self.wiki_store.get_recent_cards(limit=20)
            if cards:
                wiki_topics = "\n".join(
                    f"- [{c['page_type']}] {c['title']}"
                    for c in cards
                )
        if self.learning_profile:
            goals = self.learning_profile.get_goals()
            user_goal = "; ".join(goals) if goals else "General technical interview preparation"

        topic = (topic or "machine learning").strip()
        difficulty = difficulty or "medium"
        question_type = question_type or "conceptual"

        prompt = GENERATE_QUESTION_PROMPT.format(
            user_goal=user_goal,
            wiki_topics=wiki_topics,
            topic=topic,
            difficulty=difficulty,
            question_type=question_type,
        )

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[InterviewCoach] Generate question failed: {exc}")
            return {
                "question": f"Explain the key concepts of {topic}.",
                "topic": topic,
                "key_points": ["definition", "key mechanisms", "practical applications"],
                "ideal_answer_brief": f"A concise explanation of {topic} covering its definition, core mechanisms, and real-world applications.",
            }

        parsed = self._parse_json(raw)
        if not parsed:
            return {
                "question": f"Explain the key concepts of {topic}.",
                "topic": topic,
                "key_points": ["definition", "key mechanisms", "practical applications"],
                "ideal_answer_brief": f"A concise explanation of {topic}.",
            }

        return {
            "question": parsed.get("question", f"Explain {topic}."),
            "topic": parsed.get("topic", topic),
            "key_points": parsed.get("key_points", []),
            "ideal_answer_brief": parsed.get("ideal_answer_brief", ""),
        }

    def evaluate_answer(
        self,
        question: str,
        user_answer: str,
        key_points: List[str] = None,
        ideal_answer: str = "",
    ) -> Dict[str, Any]:
        """Evaluate the user's answer with scoring and detailed feedback."""
        key_points_str = "\n".join(f"- {p}" for p in (key_points or []))

        prompt = EVALUATE_ANSWER_PROMPT.format(
            question=question,
            user_answer=self._truncate(user_answer, 2000),
            key_points=key_points_str or "No reference points available",
            ideal_answer=ideal_answer or "No reference answer available",
        )

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[InterviewCoach] Evaluation failed: {exc}")
            return {
                "score": 5.0,
                "strengths": ["Attempted the question"],
                "weaknesses": ["Could not evaluate automatically"],
                "missing_concepts": [],
                "better_answer": "",
                "follow_up_question": "",
            }

        parsed = self._parse_json(raw)
        if not parsed:
            return {
                "score": 5.0,
                "strengths": [],
                "weaknesses": ["Could not parse evaluation"],
                "missing_concepts": [],
                "better_answer": "",
                "follow_up_question": "",
            }

        return {
            "score": float(parsed.get("score", 5)),
            "strengths": parsed.get("strengths", []),
            "weaknesses": parsed.get("weaknesses", []),
            "missing_concepts": parsed.get("missing_concepts", []),
            "better_answer": parsed.get("better_answer", ""),
            "follow_up_question": parsed.get("follow_up_question", ""),
        }

    def generate_follow_up(
        self,
        weak_points: List[str],
        previous_question: str,
        topic: str = "",
    ) -> str:
        """Generate a follow-up question targeting weak areas."""
        prompt = GENERATE_FOLLOW_UP_PROMPT.format(
            weak_points=", ".join(weak_points) if weak_points else "general depth",
            previous_question=previous_question,
            topic=topic or "the topic",
        )
        try:
            raw = self.llm.invoke(prompt)
        except Exception:
            return f"Can you elaborate more on the concepts you mentioned regarding {topic}?"

        parsed = self._parse_json(raw)
        if parsed and parsed.get("question"):
            return parsed["question"]
        return f"Can you elaborate more on the concepts you mentioned regarding {topic}?"

    def identify_weak_points(self, recent_evaluations: List[Dict]) -> List[str]:
        """Aggregate weaknesses from recent evaluations into themes."""
        all_weaknesses = []
        for ev in (recent_evaluations or []):
            all_weaknesses.extend(ev.get("weaknesses", []))
            all_weaknesses.extend(ev.get("missing_concepts", []))

        if not all_weaknesses:
            return []

        # Simple deduplication by similarity
        unique = list(dict.fromkeys(all_weaknesses))
        return unique[:8]

    def generate_review_plan(self, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """Generate a daily review plan based on weak points."""
        weak_points = []
        if self.learning_profile:
            weak_points = self.learning_profile.get_weak_points()
            due = self.learning_profile.get_due_review_topics()

        plan = []
        if weak_points:
            for i, wp in enumerate(weak_points[:days_ahead]):
                plan.append({
                    "day": i + 1,
                    "topic": wp.get("topic", ""),
                    "reason": f"Average score: {wp.get('avg_score', 0):.1f}/10",
                    "type": "weak_point_review",
                })
        return plan

    @staticmethod
    def _truncate(text: str, limit: int = 500) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    @staticmethod
    def _parse_json(text: Any) -> Optional[Dict[str, Any]]:
        text = str(text or "").strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
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
