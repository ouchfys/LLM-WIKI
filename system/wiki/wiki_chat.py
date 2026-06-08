"""Chat with the user's private Wiki."""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional

from system.memory.profile_signal_extractor import ProfileSignalExtractor
from system.wiki.maintenance.query_archive import QueryArchive


@dataclass
class WikiCitation:
    card_id: str
    title: str
    page_type: str
    summary: str
    markdown_path: str


@dataclass
class WikiChatResult:
    answer: str
    citations: List[WikiCitation] = field(default_factory=list)
    resources: List[Dict[str, str]] = field(default_factory=list)
    profile_updates: List[Dict[str, str]] = field(default_factory=list)
    tool_plan: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallPlan:
    name: str
    query: str
    reason: str = ""


@dataclass
class WikiToolPlan:
    intent: str = "answer_from_private_wiki"
    answer_mode: str = "wiki_first"
    tools: List[ToolCallPlan] = field(default_factory=list)
    use_wiki: bool = True
    use_web: bool = False
    use_resources: bool = False
    open_cards: bool = True


@dataclass
class AgentToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class AgentToolObservation:
    tool: str
    query: str
    status: str
    summary: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)


class WikiChatService:
    def __init__(
        self,
        wiki_store,
        learning_profile=None,
        session_store=None,
        llm=None,
        chunk_index=None,
        web_search=None,
        web_fetch=None,
        resource_recommender=None,
    ):
        self.wiki_store = wiki_store
        self.learning_profile = learning_profile
        self.session_store = session_store
        self.llm = llm
        self.chunk_index = chunk_index
        self.web_search = web_search
        self.web_fetch = web_fetch
        self.resource_recommender = resource_recommender
        self.profile_extractor = ProfileSignalExtractor()

    def chat(self, message: str, session_id: str = "", limit: int = 6) -> WikiChatResult:
        message = (message or "").strip()
        if not message:
            return WikiChatResult(answer="你可以问我：我之前记录过哪些 RAG 评估内容？或者把某个主题整理成面试回答。")

        history = self._load_history(session_id)
        effective_query = self._effective_query(message, history)
        tool_run = self._run_tool_loop(message, effective_query, history, limit=limit)
        plan = tool_run["plan"]
        cards = tool_run["cards"]
        web_results = tool_run["web_results"]
        resources = tool_run["resources"]
        citations = [self._citation(card) for card in cards]
        tool_plan_payload = self._plan_payload(plan)
        trace = tool_run["trace"]
        answer = self._answer(
            message,
            cards,
            history,
            web_results,
            resources,
            effective_query,
            plan,
            tool_observations=trace.get("tool_observations", []),
        )
        profile_updates = self._update_profile_from_message(message, answer, cards)
        self._save_turn(session_id, message, answer, citations, resources, profile_updates, tool_plan_payload, trace)
        return WikiChatResult(
            answer=answer,
            citations=citations,
            resources=resources,
            profile_updates=profile_updates,
            tool_plan=tool_plan_payload,
            trace=trace,
        )

    def chat_stream(self, message: str, session_id: str = "", limit: int = 6) -> Generator[dict, None, None]:
        """Stream wiki chat with agentic tool routing."""
        message = (message or "").strip()
        if not message:
            yield {"type": "token", "text": "你可以问我：我之前记录过哪些 RAG 评估内容？或者把某个主题整理成面试回答。"}
            yield {"type": "done"}
            return

        history = self._load_history(session_id)
        effective_query = self._effective_query(message, history)
        if effective_query != message:
            yield {
                "type": "tool_status",
                "tool": "context",
                "label": "Conversation Context",
                "status": "done",
                "detail": "resolved the follow-up query from recent turns",
            }

        tool_run = self._run_tool_loop(message, effective_query, history, limit=limit)
        plan = tool_run["plan"]
        cards: List[Dict[str, Any]] = tool_run["cards"]
        web_results: List[Any] = tool_run["web_results"]
        resources: List[Dict[str, str]] = tool_run["resources"]
        trace = tool_run["trace"]
        yield {"type": "tool_plan", "plan": self._plan_payload(plan)}

        for event in tool_run.get("events", []):
            yield event

        citations = [self._citation(card) for card in cards]
        yield {"type": "card_list", "citations": [c.__dict__ for c in citations]}
        if resources:
            yield {"type": "resource_list", "resources": resources}

        yield {
            "type": "agent_trace",
            "trace": trace,
        }

        full_text = ""
        if self.llm:
            prompt = self._build_prompt(
                message,
                cards,
                history,
                web_results,
                resources,
                effective_query,
                plan,
                tool_observations=trace.get("tool_observations", []),
            )
            try:
                for token in self.llm.stream_invoke(prompt, temperature=0.1, max_tokens=1200):
                    full_text += token
                    yield {"type": "token", "text": token}
            except Exception as exc:
                print(f"[WikiChatService] stream_invoke failed: {exc}, trying invoke...")
                try:
                    full_text = self.llm.invoke(prompt, temperature=0.1, max_tokens=1200).strip()
                    if full_text:
                        yield {"type": "token", "text": full_text}
                except Exception as exc2:
                    print(f"[WikiChatService] invoke also failed: {exc2}")
                    full_text = self._fallback_answer(cards, web_results, resources)
                    yield {"type": "token", "text": full_text}
        else:
            full_text = self._fallback_answer(cards, web_results, resources)
            yield {"type": "token", "text": full_text}

        profile_updates = self._update_profile_from_message(message, full_text, cards)
        if profile_updates:
            yield {"type": "profile", "updates": profile_updates}

        self._save_turn(
            session_id,
            message,
            full_text,
            citations,
            resources,
            profile_updates,
            self._plan_payload(plan),
            trace,
        )
        yield {"type": "done"}

    def _plan_tools(self, message: str, effective_query: str, history: List) -> WikiToolPlan:
        fallback = self._fallback_tool_plan(message, effective_query)
        if not self.llm:
            return fallback

        prompt = (
            "Return a strict JSON object for routing a private Wiki chat. Do not answer the user.\n"
            "Allowed tools: wiki_search, wiki_card, web_search, resource_recommend.\n"
            "Rules:\n"
            "- Prefer wiki_search/wiki_card for stable concepts, paper notes, and interview prep already in the user's Wiki.\n"
            "- Use web_search only for latest/current/mainstream status, GitHub/arXiv/source discovery, or when private Wiki is likely missing.\n"
            "- Use resource_recommend only when the user asks for papers, tutorials, videos, links, or follow-up reading.\n"
            "- wiki_card means opening the strongest returned Wiki cards as skill-like memory, not raw chunk dumping.\n"
            "Schema: {\"intent\": string, \"answer_mode\": string, \"tools\": [{\"name\": string, \"query\": string, \"reason\": string}]}.\n\n"
            f"User message: {message}\n"
            f"Effective query: {effective_query}\n"
            f"Recent turns: {history[-2:] if history else []}\n"
        )
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=400).strip()
            parsed = self._parse_json_object(raw)
            plan = self._normalize_tool_plan(parsed, effective_query)
            if plan.tools:
                return plan
        except Exception as exc:
            print(f"[WikiChatService] tool planning failed: {exc}")
        return fallback

    def _fallback_tool_plan(self, message: str, effective_query: str) -> WikiToolPlan:
        query = effective_query or message
        use_web = self._should_search_web(query)
        use_resources = self._should_recommend_resources(query)
        tools = [
            ToolCallPlan("wiki_search", query, "retrieve stable private Wiki memory"),
            ToolCallPlan("wiki_card", query, "open matching Wiki cards as reusable skills"),
        ]
        if use_web:
            tools.append(ToolCallPlan("web_search", self._web_search_query(query), "freshness or external source check"))
        if use_resources:
            tools.append(ToolCallPlan("resource_recommend", query, "follow-up learning resources requested"))
        return WikiToolPlan(
            intent="answer_from_private_wiki_with_optional_tools",
            answer_mode="wiki_first_then_external_check" if use_web else "wiki_first",
            tools=tools,
            use_wiki=True,
            use_web=use_web,
            use_resources=use_resources,
            open_cards=True,
        )

    def _run_tool_loop(
        self,
        message: str,
        effective_query: str,
        history: List,
        limit: int = 6,
        max_steps: int = 3,
    ) -> Dict[str, Any]:
        """Run a Claude-Code-style plan -> tool call -> observation loop.

        The LLM only emits JSON tool calls. Python validates and executes every
        tool, then feeds compact observations back into the next planning step.
        """
        cards: List[Dict[str, Any]] = []
        web_results: List[Any] = []
        resources: List[Dict[str, str]] = []
        observations: List[AgentToolObservation] = []
        executed_calls: List[ToolCallPlan] = []
        events: List[Dict[str, Any]] = []

        seen_signatures: set[str] = set()
        used_llm_step = False
        if self.llm:
            for step_index in range(max_steps):
                tool_calls = self._next_agent_tool_calls(
                    message=message,
                    effective_query=effective_query,
                    history=history,
                    observations=observations,
                    step_index=step_index,
                    limit=limit,
                )
                if not tool_calls:
                    break
                used_llm_step = True
                for call in tool_calls:
                    signature = self._tool_signature(call)
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    observation = self._execute_agent_tool_call(
                        call=call,
                        cards=cards,
                        web_results=web_results,
                        resources=resources,
                        limit=limit,
                    )
                    observations.append(observation)
                    executed_calls.append(ToolCallPlan(
                        name=call.name,
                        query=str(call.arguments.get("query") or ""),
                        reason=call.reason,
                    ))
                    events.append(self._tool_status_event(observation))
                if any(call.name in {"web_fetch", "resource_recommend"} for call in tool_calls):
                    break

        if not executed_calls:
            fallback = self._fallback_tool_plan(message, effective_query)
            for call_plan in fallback.tools:
                call = AgentToolCall(
                    name=call_plan.name,
                    arguments={"query": call_plan.query, "limit": limit},
                    reason=call_plan.reason,
                )
                observation = self._execute_agent_tool_call(
                    call=call,
                    cards=cards,
                    web_results=web_results,
                    resources=resources,
                    limit=limit,
                )
                observations.append(observation)
                executed_calls.append(call_plan)
                events.append(self._tool_status_event(observation))

        if (
            cards
            or web_results
            or self._is_private_scope_query(message)
            or not self.web_search
            or not getattr(self.web_search, "available", False)
        ):
            pass
        else:
            call = AgentToolCall(
                name="web_search",
                arguments={"query": effective_query or message, "limit": 5},
                reason="wiki tools returned no strong observation",
            )
            observation = self._execute_agent_tool_call(
                call=call,
                cards=cards,
                web_results=web_results,
                resources=resources,
                limit=limit,
            )
            observations.append(observation)
            executed_calls.append(ToolCallPlan(call.name, call.arguments["query"], call.reason))
            events.append(self._tool_status_event(observation))

        if (
            web_results
            and self.web_fetch
            and getattr(self.web_fetch, "available", False)
            and any(call.name == "web_search" for call in executed_calls)
            and not any(call.name == "web_fetch" for call in executed_calls)
            and not any(self._web_result_has_fetched_content(item) for item in web_results)
        ):
            url = self._first_unfetched_web_url(web_results)
            if url:
                call = AgentToolCall(
                    name="web_fetch",
                    arguments={"query": effective_query or message, "url": url, "limit": min(limit, 4)},
                    reason="auto-fetch top web result so web evidence includes readable passages",
                )
                observation = self._execute_agent_tool_call(
                    call=call,
                    cards=cards,
                    web_results=web_results,
                    resources=resources,
                    limit=limit,
                )
                observations.append(observation)
                executed_calls.append(ToolCallPlan(call.name, call.arguments["query"], call.reason))
                events.append(self._tool_status_event(observation))

        plan = self._tool_plan_from_calls(executed_calls, effective_query, used_llm_step)
        trace = self._trace_payload(
            plan,
            cards,
            web_results,
            resources,
            tool_observations=[self._observation_payload(item) for item in observations],
        )
        return {
            "plan": plan,
            "cards": cards,
            "web_results": web_results,
            "resources": resources,
            "trace": trace,
            "events": events,
        }

    def _next_agent_tool_calls(
        self,
        message: str,
        effective_query: str,
        history: List,
        observations: List[AgentToolObservation],
        step_index: int,
        limit: int,
    ) -> List[AgentToolCall]:
        native_calls = self._next_native_tool_calls(
            message=message,
            effective_query=effective_query,
            history=history,
            observations=observations,
            step_index=step_index,
            limit=limit,
        )
        if native_calls is not None:
            return native_calls

        prompt = self._tool_loop_prompt(
            message=message,
            effective_query=effective_query,
            history=history,
            observations=observations,
            step_index=step_index,
            limit=limit,
        )
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=900).strip()
            parsed = self._parse_json_object(raw)
        except Exception as exc:
            print(f"[WikiChatService] tool loop planning failed: {exc}")
            return []
        return self._normalize_agent_tool_calls(parsed, effective_query, limit)

    def _next_native_tool_calls(
        self,
        message: str,
        effective_query: str,
        history: List,
        observations: List[AgentToolObservation],
        step_index: int,
        limit: int,
    ) -> Optional[List[AgentToolCall]]:
        if not self.llm or not hasattr(self.llm, "tool_call"):
            return None
        messages = self._native_tool_messages(
            message=message,
            effective_query=effective_query,
            history=history,
            observations=observations,
            step_index=step_index,
            limit=limit,
        )
        try:
            raw_message = self.llm.tool_call(
                messages=messages,
                tools=self._native_tool_specs(),
                tool_choice="auto",
                temperature=0.0,
                max_tokens=600,
            )
        except Exception as exc:
            print(f"[WikiChatService] native tool calling failed, falling back to JSON routing: {exc}")
            return None
        return self._normalize_native_tool_calls(raw_message, effective_query, limit)

    def _native_tool_messages(
        self,
        message: str,
        effective_query: str,
        history: List,
        observations: List[AgentToolObservation],
        step_index: int,
        limit: int,
    ) -> List[Dict[str, str]]:
        observation_text = self._observation_context(observations)
        history_text = "\n".join(f"User: {q}\nAssistant: {a}" for q, a in history[-2:])
        system_text = (
            "You are the tool-use controller for a private Wiki assistant. "
            "Do not answer the user in natural language. Decide whether the next step needs a tool call.\n"
            "Rules:\n"
            "- Prefer wiki_search first for private notes, papers, concepts, methods, and interview prep.\n"
            "- Use wiki_card after wiki_search when you need to open matched cards as evidence.\n"
            "- Use web_search only for latest/current/source discovery or when Wiki observations are weak.\n"
            "- If the user provides a concrete URL and asks to inspect it, call web_fetch directly with that URL.\n"
            "- Use web_fetch after web_search to open a concrete URL before treating web information as evidence.\n"
            "- Use resource_recommend only when the user asks for follow-up papers, tutorials, videos, links, or study resources.\n"
            "- If observations are enough, return no tool calls.\n"
        )
        user_text = (
            f"Step: {step_index + 1}\n"
            f"Default limit: {limit}\n"
            f"User message: {message}\n"
            f"Effective query: {effective_query}\n"
            f"Recent turns:\n{history_text or '(none)'}\n\n"
            f"Previous observations:\n{observation_text}\n"
        )
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

    @staticmethod
    def _native_tool_specs() -> List[Dict[str, Any]]:
        def schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
            return {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }

        query_limit = {
            "query": {"type": "string", "description": "Search query or user intent."},
            "limit": {"type": "integer", "description": "Maximum number of items to return.", "minimum": 1, "maximum": 8},
        }
        return [
            {
                "type": "function",
                "function": {
                    "name": "wiki_search",
                    "description": "Search private Wiki chunks and cards.",
                    "parameters": schema(query_limit, ["query"]),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wiki_card",
                    "description": "Open matched Wiki cards as structured memory.",
                    "parameters": schema(query_limit, ["query"]),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the public web for temporary external references.",
                    "parameters": schema(query_limit, ["query"]),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "Open a specific public URL and extract readable passages for evidence.",
                    "parameters": schema(
                        {
                            "url": {"type": "string", "description": "Public URL to fetch."},
                            "query": {"type": "string", "description": "User question used to rank passages."},
                            "limit": {"type": "integer", "description": "Maximum number of passages.", "minimum": 1, "maximum": 8},
                        },
                        ["url"],
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resource_recommend",
                    "description": "Find papers, videos, and posts for follow-up learning.",
                    "parameters": schema(query_limit, ["query"]),
                },
            },
        ]

    @staticmethod
    def _normalize_native_tool_calls(
        data: Dict[str, Any],
        default_query: str,
        default_limit: int,
    ) -> List[AgentToolCall]:
        if not isinstance(data, dict):
            return []
        tool_calls = data.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            return []
        allowed = {"wiki_search", "wiki_card", "web_search", "web_fetch", "resource_recommend"}
        result: List[AgentToolCall] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(function.get("name") or item.get("name") or "").strip()
            if name not in allowed:
                continue
            raw_args = function.get("arguments") or item.get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            query = str(args.get("query") or default_query).strip()
            url = str(args.get("url") or "").strip()
            try:
                limit = int(args.get("limit") or default_limit)
            except (TypeError, ValueError):
                limit = default_limit
            result.append(AgentToolCall(
                name=name,
                arguments={"query": query, "url": url, "limit": max(1, min(limit, 8))},
                reason="native function calling",
            ))
        return result[:3]

    def _tool_loop_prompt(
        self,
        message: str,
        effective_query: str,
        history: List,
        observations: List[AgentToolObservation],
        step_index: int,
        limit: int,
    ) -> str:
        observation_text = self._observation_context(observations)
        history_text = "\n".join(f"User: {q}\nAssistant: {a}" for q, a in history[-2:])
        tool_specs = json.dumps(self._tool_specs(), ensure_ascii=False, indent=2)
        return (
            "You are a tool-use controller for a private Wiki assistant. "
            "Do not answer the user. Decide the next tool call only.\n"
            "Return exactly one strict JSON object. No markdown. No prose.\n"
            "Available tools are defined by this schema:\n"
            f"{tool_specs}\n\n"
            "Tool-use rules:\n"
            "- Prefer wiki_search first for private notes, papers, concepts, methods, and interview prep.\n"
            "- Use wiki_card after wiki_search when you need to open the matched cards as evidence.\n"
            "- Use web_search only for latest/current/source discovery or when Wiki observations are weak.\n"
            "- If the user provides a concrete URL and asks to inspect it, call web_fetch directly with that URL.\n"
            "- Use web_fetch after web_search to open a concrete URL before treating web information as evidence.\n"
            "- Use resource_recommend only when the user asks for follow-up papers, tutorials, videos, links, or study resources.\n"
            "- Stop by returning {\"finish\": true, \"tool_calls\": []} when observations are enough.\n"
            "JSON shape:\n"
            "{\"thought\": string, \"finish\": boolean, "
            "\"tool_calls\": [{\"name\": string, \"arguments\": {\"query\": string, \"url\": string, \"limit\": number}, \"reason\": string}]}\n\n"
            f"Step: {step_index + 1}\n"
            f"Default limit: {limit}\n"
            f"User message: {message}\n"
            f"Effective query: {effective_query}\n"
            f"Recent turns:\n{history_text or '(none)'}\n\n"
            f"Previous observations:\n{observation_text}\n"
        )

    @staticmethod
    def _tool_specs() -> List[Dict[str, Any]]:
        return [
            {
                "name": "wiki_search",
                "description": "Search private Wiki chunks and cards.",
                "arguments": {"query": "string", "limit": "integer"},
            },
            {
                "name": "wiki_card",
                "description": "Open matched Wiki cards as structured memory.",
                "arguments": {"query": "string", "limit": "integer"},
            },
            {
                "name": "web_search",
                "description": "Search the public web for temporary external references.",
                "arguments": {"query": "string", "limit": "integer"},
            },
            {
                "name": "web_fetch",
                "description": "Open a specific public URL and extract readable passages for evidence.",
                "arguments": {"url": "string", "query": "string", "limit": "integer"},
            },
            {
                "name": "resource_recommend",
                "description": "Find papers, videos, and posts for follow-up learning.",
                "arguments": {"query": "string", "limit": "integer"},
            },
        ]

    @staticmethod
    def _normalize_agent_tool_calls(
        data: Dict[str, Any],
        default_query: str,
        default_limit: int,
    ) -> List[AgentToolCall]:
        if not isinstance(data, dict) or data.get("finish") is True:
            return []
        allowed = {"wiki_search", "wiki_card", "web_search", "web_fetch", "resource_recommend"}
        raw_calls = data.get("tool_calls")
        if raw_calls is None:
            raw_calls = data.get("tools")
        result: List[AgentToolCall] = []
        for item in raw_calls or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name not in allowed:
                continue
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            query = str(args.get("query") or item.get("query") or default_query).strip()
            url = str(args.get("url") or item.get("url") or "").strip()
            try:
                limit = int(args.get("limit") or default_limit)
            except (TypeError, ValueError):
                limit = default_limit
            result.append(AgentToolCall(
                name=name,
                arguments={"query": query, "url": url, "limit": max(1, min(limit, 8))},
                reason=str(item.get("reason") or data.get("thought") or ""),
            ))
        return result[:3]

    def _execute_agent_tool_call(
        self,
        call: AgentToolCall,
        cards: List[Dict[str, Any]],
        web_results: List[Any],
        resources: List[Dict[str, str]],
        limit: int,
    ) -> AgentToolObservation:
        query = str(call.arguments.get("query") or "").strip()
        call_limit = max(1, min(int(call.arguments.get("limit") or limit), 8))
        if call.name == "wiki_search":
            found = self._retrieve_cards(query, limit=call_limit)
            self._merge_cards(cards, found)
            return AgentToolObservation(
                tool=call.name,
                query=query,
                status="done",
                summary=f"found {len(found)} wiki cards",
                items=[self._trace_card(card) for card in found[:call_limit]],
            )
        if call.name == "wiki_card":
            found = cards[:call_limit] or self._retrieve_cards(query, limit=call_limit)
            self._merge_cards(cards, found)
            return AgentToolObservation(
                tool=call.name,
                query=query,
                status="done",
                summary=f"opened {len(found)} wiki cards",
                items=[self._trace_card(card) for card in found[:call_limit]],
            )
        if call.name == "web_search":
            found = self._retrieve_web(query, cards, force=True)
            web_results[:] = self._merge_web_results(web_results, found)
            return AgentToolObservation(
                tool=call.name,
                query=query,
                status="done",
                summary=f"found {len(found)} web results",
                items=[self._trace_web_result(item) for item in found[:5]],
            )
        if call.name == "web_fetch":
            requested_url = str(call.arguments.get("url") or "").strip()
            urls = self._web_fetch_candidate_urls(web_results, preferred_url=requested_url)
            if not urls:
                return AgentToolObservation(
                    tool=call.name,
                    query=query,
                    status="error",
                    summary="no URL available for web_fetch",
                )
            if not self.web_fetch or not getattr(self.web_fetch, "available", False):
                return AgentToolObservation(
                    tool=call.name,
                    query=query or requested_url or urls[0],
                    status="error",
                    summary="web_fetch unavailable",
                )
            attempts: List[Any] = []
            errors: List[str] = []
            for url in urls[: max(1, min(call_limit, 4))]:
                try:
                    fetched = self.web_fetch.fetch(url, query=query, max_passages=min(call_limit, 4))
                except Exception as exc:
                    errors.append(f"{url}: {exc}")
                    continue
                attempts.append(fetched)
                if getattr(fetched, "status", "") == "done":
                    web_results[:] = self._merge_web_results(web_results, [fetched])
                    summary = f"fetched {getattr(fetched, 'title', '') or getattr(fetched, 'url', '')}"
                    return AgentToolObservation(
                        tool=call.name,
                        query=query or getattr(fetched, "url", "") or url,
                        status="done",
                        summary=summary[:500],
                        items=[self._trace_web_result(fetched)],
                    )
                errors.append(f"{url}: {getattr(fetched, 'error', '') or 'web_fetch failed'}")

            if requested_url and len(urls) == 1:
                status = "error"
                summary = errors[0] if errors else "web_fetch failed"
            else:
                status = "done" if web_results else "error"
                summary = (
                    f"full-page fetch blocked for {len(errors)} URL(s); using web search snippets instead"
                    if web_results
                    else (errors[0] if errors else "web_fetch failed")
                )
            return AgentToolObservation(
                tool=call.name,
                query=query or requested_url or (urls[0] if urls else ""),
                status=status,
                summary=summary[:500],
                items=[self._trace_web_result(item) for item in (attempts[:3] or web_results[:3])],
            )
        if call.name == "resource_recommend":
            found = self._recommend_resources(query, cards, force=True)
            resources[:] = self._merge_resources(resources, found)
            return AgentToolObservation(
                tool=call.name,
                query=query,
                status="done",
                summary=f"found {len(found)} learning resources",
                items=found[:6],
            )
        return AgentToolObservation(
            tool=call.name,
            query=query,
            status="error",
            summary="unknown tool",
        )

    @staticmethod
    def _merge_cards(target: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> None:
        seen = {card.get("id") for card in target}
        for card in incoming:
            card_id = card.get("id")
            if card_id and card_id not in seen:
                target.append(card)
                seen.add(card_id)

    @staticmethod
    def _merge_web_results(existing: List[Any], incoming: List[Any]) -> List[Any]:
        merged = list(existing)
        url_index = {
            str(getattr(item, "url", "") or ""): index
            for index, item in enumerate(merged)
            if getattr(item, "url", "")
        }
        for item in incoming:
            url = str(getattr(item, "url", "") or "")
            if not url:
                continue
            if url in url_index:
                existing_item = merged[url_index[url]]
                if (
                    WikiChatService._web_result_has_fetched_content(item)
                    and not WikiChatService._web_result_has_fetched_content(existing_item)
                ):
                    merged[url_index[url]] = item
            else:
                merged.append(item)
                url_index[url] = len(merged) - 1
        return merged

    @staticmethod
    def _web_result_has_fetched_content(item: Any) -> bool:
        return bool(getattr(item, "passages", None) or getattr(item, "text_excerpt", ""))

    @staticmethod
    def _first_unfetched_web_url(web_results: List[Any]) -> str:
        for item in web_results or []:
            url = str(getattr(item, "url", "") or "").strip()
            if url and not WikiChatService._web_result_has_fetched_content(item):
                return url
        return ""

    @staticmethod
    def _web_fetch_candidate_urls(web_results: List[Any], preferred_url: str = "") -> List[str]:
        urls: List[str] = []
        preferred_url = (preferred_url or "").strip()
        if preferred_url:
            urls.append(preferred_url)
        for item in web_results or []:
            url = str(getattr(item, "url", "") or "").strip()
            if not url or url in urls or WikiChatService._web_result_has_fetched_content(item):
                continue
            urls.append(url)
        return urls

    @staticmethod
    def _merge_resources(existing: List[Dict[str, str]], incoming: List[Dict[str, str]]) -> List[Dict[str, str]]:
        urls = {item.get("url", "") for item in existing}
        merged = list(existing)
        for item in incoming:
            url = item.get("url", "")
            if url and url not in urls:
                merged.append(item)
                urls.add(url)
        return merged

    @staticmethod
    def _tool_signature(call: AgentToolCall) -> str:
        query = str(call.arguments.get("query") or "").strip().lower()
        if call.name == "web_fetch":
            url = str(call.arguments.get("url") or "").strip().lower()
            return f"{call.name}:{url or query}"
        return f"{call.name}:{query}"

    @staticmethod
    def _observation_payload(observation: AgentToolObservation) -> Dict[str, Any]:
        return {
            "tool": observation.tool,
            "query": observation.query,
            "status": observation.status,
            "summary": observation.summary,
            "items": observation.items,
        }

    @staticmethod
    def _observation_context(observations: List[AgentToolObservation]) -> str:
        if not observations:
            return "(none)"
        lines: List[str] = []
        for index, observation in enumerate(observations[-6:], start=1):
            lines.append(
                f"[Observation {index}] tool={observation.tool}; query={observation.query}; "
                f"status={observation.status}; summary={observation.summary}"
            )
            for item in observation.items[:3]:
                title = item.get("title") or item.get("url") or item.get("card_id") or ""
                url = item.get("url") or ""
                passages = item.get("passages") or []
                snippet = item.get("summary") or item.get("snippet") or item.get("text_excerpt") or ""
                if passages and isinstance(passages[0], dict):
                    snippet = passages[0].get("text") or snippet
                detail = f"{str(snippet)[:220]}"
                if url:
                    detail = f"url={url}; {detail}"
                lines.append(f"- {title}: {detail}")
        return "\n".join(lines)

    @staticmethod
    def _answer_observation_context(observations: List[Dict[str, Any]]) -> str:
        if not observations:
            return "(none)"
        lines: List[str] = []
        for index, observation in enumerate(observations[-8:], start=1):
            if not isinstance(observation, dict):
                continue
            lines.append(
                f"[Observation {index}] tool={observation.get('tool', '')}; "
                f"query={observation.get('query', '')}; summary={observation.get('summary', '')}"
            )
            for item in (observation.get("items") or [])[:4]:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("url") or item.get("card_id") or ""
                passages = item.get("passages") or []
                detail = item.get("summary") or item.get("snippet") or item.get("text_excerpt") or item.get("page_type") or ""
                if passages and isinstance(passages[0], dict):
                    detail = passages[0].get("text") or detail
                if title or detail:
                    lines.append(f"- {title}: {str(detail)[:260]}")
        return "\n".join(lines) if lines else "(none)"

    @staticmethod
    def _tool_status_event(observation: AgentToolObservation) -> Dict[str, Any]:
        labels = {
            "wiki_search": "Wiki Search",
            "wiki_card": "Wiki Card",
            "web_search": "Web Search",
            "web_fetch": "Web Fetch",
            "resource_recommend": "Resource Recommend",
        }
        return {
            "type": "tool_status",
            "tool": observation.tool,
            "label": labels.get(observation.tool, observation.tool),
            "status": "done" if observation.status == "done" else "error",
            "detail": observation.summary,
        }

    @staticmethod
    def _tool_plan_from_calls(
        calls: List[ToolCallPlan],
        default_query: str,
        used_llm_step: bool,
    ) -> WikiToolPlan:
        names = {call.name for call in calls}
        return WikiToolPlan(
            intent="tool_use_agent_answer",
            answer_mode="plan_call_observe_answer" if used_llm_step else "fallback_plan_call_observe_answer",
            tools=calls or [ToolCallPlan("wiki_search", default_query, "default private Wiki lookup")],
            use_wiki=bool({"wiki_search", "wiki_card"} & names) or not names,
            use_web=bool({"web_search", "web_fetch"} & names),
            use_resources="resource_recommend" in names,
            open_cards="wiki_card" in names,
        )

    def _normalize_tool_plan(self, data: Dict[str, Any], default_query: str) -> WikiToolPlan:
        allowed = {"wiki_search", "wiki_card", "web_search", "web_fetch", "resource_recommend"}
        calls: List[ToolCallPlan] = []
        for item in data.get("tools", []) if isinstance(data, dict) else []:
            name = str(item.get("name", "")).strip()
            if name not in allowed:
                continue
            calls.append(ToolCallPlan(
                name=name,
                query=str(item.get("query") or default_query),
                reason=str(item.get("reason") or ""),
            ))
        names = {call.name for call in calls}
        if "wiki_search" not in names:
            calls.insert(0, ToolCallPlan("wiki_search", default_query, "default private Wiki lookup"))
            names.add("wiki_search")
        if "wiki_card" not in names:
            calls.insert(1, ToolCallPlan("wiki_card", default_query, "open matched Wiki cards"))
            names.add("wiki_card")
        return WikiToolPlan(
            intent=str(data.get("intent") or "answer_from_private_wiki") if isinstance(data, dict) else "answer_from_private_wiki",
            answer_mode=str(data.get("answer_mode") or "wiki_first") if isinstance(data, dict) else "wiki_first",
            tools=calls,
            use_wiki="wiki_search" in names or "wiki_card" in names,
            use_web=bool({"web_search", "web_fetch"} & names),
            use_resources="resource_recommend" in names,
            open_cards="wiki_card" in names,
        )

    @staticmethod
    def _parse_json_object(text: str) -> Dict[str, Any]:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        return json.loads(match.group(0) if match else cleaned)

    @staticmethod
    def _plan_payload(plan: WikiToolPlan) -> Dict[str, Any]:
        return {
            "intent": plan.intent,
            "answer_mode": plan.answer_mode,
            "use_wiki": plan.use_wiki,
            "use_web": plan.use_web,
            "use_resources": plan.use_resources,
            "open_cards": plan.open_cards,
            "tools": [call.__dict__ for call in plan.tools],
        }

    def _trace_payload(
        self,
        plan: WikiToolPlan,
        cards: List[Dict[str, Any]],
        web_results: List[Any],
        resources: List[Dict[str, str]],
        tool_observations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return {
            "tool_plan": self._plan_payload(plan),
            "tool_observations": tool_observations or [],
            "retrieved_cards": [self._trace_card(card) for card in cards[:6]],
            "web_results": [self._trace_web_result(item) for item in (web_results or [])[:5]],
            "resources": resources or [],
            "diagnostics": {
                "wiki_card_count": len(cards or []),
                "web_result_count": len(web_results or []),
                "resource_count": len(resources or []),
            },
        }

    @staticmethod
    def _trace_card(card: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "card_id": card.get("id", ""),
            "title": card.get("title", ""),
            "page_type": card.get("page_type", ""),
            "summary": card.get("summary", ""),
            "markdown_path": card.get("markdown_path", ""),
            "matched_chunks": [str(item)[:600] for item in (card.get("_matched_chunks") or [])[:3]],
        }

    @staticmethod
    def _trace_web_result(item: Any) -> Dict[str, Any]:
        passages = []
        for passage in (getattr(item, "passages", None) or [])[:4]:
            if isinstance(passage, dict):
                passages.append({
                    "rank": passage.get("rank"),
                    "score": passage.get("score"),
                    "text": str(passage.get("text", "") or "")[:900],
                })
        return {
            "title": str(getattr(item, "title", "") or ""),
            "url": str(getattr(item, "url", "") or ""),
            "site": str(getattr(item, "site", "") or ""),
            "snippet": str(getattr(item, "snippet", "") or "")[:500],
            "text_excerpt": str(getattr(item, "text_excerpt", "") or "")[:1200],
            "passages": passages,
            "published_at": str(getattr(item, "published_at", "") or ""),
            "author": str(getattr(item, "author", "") or ""),
            "fetched_at": str(getattr(item, "fetched_at", "") or ""),
            "status": str(getattr(item, "status", "") or ""),
            "error": str(getattr(item, "error", "") or "")[:500],
            "fetched": WikiChatService._web_result_has_fetched_content(item),
        }

    def _tool_plan_context(self, plan: Optional[WikiToolPlan]) -> str:
        if not plan:
            return "(no explicit plan)"
        lines = [
            f"intent: {plan.intent}",
            f"answer_mode: {plan.answer_mode}",
            f"use_wiki: {plan.use_wiki}; use_web: {plan.use_web}; use_resources: {plan.use_resources}; open_cards: {plan.open_cards}",
            "tool_calls:",
        ]
        for call in plan.tools:
            lines.append(f"- {call.name}: query={call.query}; reason={call.reason}")
        return "\n".join(lines)

    @staticmethod
    def _tool_query(plan: WikiToolPlan, name: str, default_query: str) -> str:
        for call in plan.tools:
            if call.name == name and (call.query or "").strip():
                return call.query.strip()
        return default_query

    @staticmethod
    def _is_private_scope_query(message: str) -> bool:
        text = (message or "").lower()
        private_scope = [
            "\u6211\u5e93\u91cc", "\u6211\u7684\u5e93", "\u6211\u7684wiki", "\u6211\u7684 wiki",
            "\u6211\u7684\u7b14\u8bb0", "\u77e5\u8bc6\u5e93", "my wiki", "my notes", "private wiki",
        ]
        explicit_external = [
            "\u6700\u65b0", "\u6700\u8fd1", "\u73b0\u5728", "\u5f53\u524d", "\u8054\u7f51",
            "\u641c\u7d22", "\u67e5\u4e00\u4e0b", "\u627e\u4e00\u4e0b", "\u5f00\u6e90",
            "web", "search", "google", "github", "arxiv", "sota", "leaderboard",
        ]
        return any(marker in text for marker in private_scope) and not any(marker in text for marker in explicit_external)

    def _retrieve_cards(self, message: str, limit: int) -> List[Dict[str, Any]]:
        # 1. Try unified chunk search first
        if self.chunk_index:
            chunks = self.chunk_index.search(message, limit=limit * 5)
            chunks = self._filter_relevant_chunks(message, chunks)
            if chunks:
                return self._cards_from_chunks(chunks, limit)

        # 2. Fallback to card title/summary search
        cards = self.wiki_store.search_cards(message, limit=limit)
        cards = self._filter_relevant_cards(message, cards)
        if cards:
            return self._dedupe_cards(cards)[:limit]

        # 3. Keyword scan with relevance threshold
        terms = self._terms(message)
        scored = []
        for card in self.wiki_store.get_recent_cards(limit=200):
            text = self._card_text(card).lower()
            score = sum(1 for term in terms if self._is_meaningful_term(term) and term.lower() in text)
            if score >= 2:
                scored.append((score, card))
        scored.sort(key=lambda item: item[0], reverse=True)
        return self._dedupe_cards([card for _, card in scored])[:limit]

    def _retrieve_web(self, message: str, cards: List[Dict[str, Any]], force: bool = False) -> List[Any]:
        if not self.web_search or not getattr(self.web_search, "available", False):
            return []
        if cards and not force and not self._should_search_web(message):
            return []
        try:
            return self.web_search.search(self._web_search_query(message), limit=5)
        except Exception as exc:
            print(f"[WikiChatService] web search failed: {exc}")
            return []

    def _recommend_resources(self, message: str, cards: List[Dict[str, Any]], force: bool = False) -> List[Dict[str, str]]:
        if not self.resource_recommender or not getattr(self.resource_recommender, "available", False):
            return []
        if cards and not force and not self._should_search_web(message):
            return []
        try:
            return [
                item.__dict__
                for item in self.resource_recommender.recommend(message, limit_per_category=2)
            ]
        except Exception as exc:
            print(f"[WikiChatService] resource recommendation failed: {exc}")
            return []

    def _cards_from_chunks(self, chunks: List[Dict], limit: int) -> List[Dict[str, Any]]:
        """Dedupe by card_id, enrich cards with matched chunk snippets."""
        seen = set()
        result: List[Dict[str, Any]] = []
        chunk_snippets: Dict[str, List[str]] = {}

        for chunk in chunks:
            cid = chunk["card_id"]
            if cid not in chunk_snippets:
                chunk_snippets[cid] = []
            chunk_snippets[cid].append(chunk["text"][:300])

        for chunk in chunks:
            cid = chunk["card_id"]
            if cid in seen:
                continue
            seen.add(cid)

            card = self.wiki_store.get_card(cid)
            if card:
                # Attach matched chunk snippets for the prompt
                card["_matched_chunks"] = chunk_snippets.get(cid, [])[:3]
                result.append(card)

            if len(result) >= limit:
                break

        return result

    def _answer(
        self,
        message: str,
        cards: List[Dict[str, Any]],
        history: List,
        web_results: List[Any],
        resources: List[Dict[str, str]],
        effective_query: str = "",
        tool_plan: Optional[WikiToolPlan] = None,
        tool_observations: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if self.llm:
            prompt = self._build_prompt(
                message,
                cards,
                history,
                web_results,
                resources,
                effective_query,
                tool_plan,
                tool_observations=tool_observations,
            )
            try:
                return self.llm.invoke(prompt, temperature=0.1, max_tokens=1200).strip()
            except Exception as exc:
                print(f"[WikiChatService] LLM answer failed: {exc}")
        return self._fallback_answer(cards, web_results, resources)

    def _fallback_answer(
        self,
        cards: List[Dict[str, Any]],
        web_results: List[Any] = None,
        resources: List[Dict[str, str]] = None,
    ) -> str:
        if not cards:
            if web_results:
                lines = ["我在你的 Wiki 里没有找到强相关笔记，下面是临时联网检索到的参考：", ""]
                for index, item in enumerate(web_results[:4], start=1):
                    lines.append(f"{index}. {getattr(item, 'title', '')}")
                    if getattr(item, "snippet", ""):
                        lines.append(f"   {getattr(item, 'snippet', '')}")
                    lines.append(f"   {getattr(item, 'url', '')}")
                return "\n".join(lines)
            return (
                "我在你的 Wiki 里没有检索到强相关内容。"
                "但这个问题可以先按通用知识回答；如果你希望沉淀为个人知识，再把相关论文、博客或面经存入 Wiki。"
            )
        lines = ["我在你的 Wiki 里找到了这些相关内容：", ""]
        for index, card in enumerate(cards[:4], start=1):
            lines.append(f"{index}. **{card['title']}**")
            if card.get("summary"):
                lines.append(f"   {card['summary']}")
        lines.extend(["", "基于当前资料，建议你优先打开引用笔记继续整理；如果要面试表达，可以让我把这些内容改写成问答版。"])
        return "\n".join(lines)

    def _build_prompt(
        self,
        message: str,
        cards: List[Dict[str, Any]],
        history: List,
        web_results: List[Any] = None,
        resources: List[Dict[str, str]] = None,
        effective_query: str = "",
        tool_plan: Optional[WikiToolPlan] = None,
        tool_observations: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        context_blocks = []
        for index, card in enumerate(cards, start=1):
            parts = [
                f"[{index}] {card.get('title', '')}",
                f"type: {card.get('page_type', '')}",
                f"summary: {card.get('summary', '')}",
            ]
            # Prefer matched chunk snippets over compacted content_json
            matched = card.get("_matched_chunks")
            if matched:
                parts.append("matched passages:")
                for ci, snippet in enumerate(matched, start=1):
                    parts.append(f"  passage {ci}: {snippet[:600]}")
            else:
                content = card.get("content_json") or {}
                parts.append(f"content: {self._compact_content(content)}")
            parts.append(f"path: {card.get('markdown_path', '')}")
            context_blocks.append("\n".join(parts))
        history_text = "\n".join(f"User: {q}\nAssistant: {a}" for q, a in history[-3:])
        profile_text = self._profile_context()
        memory_text = self._episode_context(message)
        web_text = self._web_context(web_results or [])
        resource_text = self._resource_context(resources or [])
        tool_text = self._tool_plan_context(tool_plan)
        observation_text = self._answer_observation_context(tool_observations or [])
        tool_policy = (
            "You are the user's private Wiki assistant. Treat tools as explicit capabilities, not a fixed RAG pipeline.\n"
            "The runtime follows a plan-call-observe-answer loop: the model proposes tool calls, Python executes them, "
            "and observations below are the only executed tool results.\n"
            "Wiki Search/Wiki Cards are stable personal memory and should be the primary source. "
            "Web Search discovers public links; Web Fetch opens a URL and supplies citeable external passages. "
            "Web evidence is temporary context for freshness, missing coverage, or source discovery; "
            "do not merge web facts into Wiki unless the user imports them. "
            "Resource Recommend is only for follow-up reading.\n"
            "Answer in Chinese. Lead with the conclusion, then give structured reasoning. Cite Wiki cards as [1] [2]. "
            "Cite Web Search/Web Fetch as [W1] [W2] and list titles/URLs when used. If Wiki cards are weak, say so directly.\n\n"
            "Evidence discipline:\n"
            "- If card [1] directly matches the user's question, treat [1] as the main evidence and use later cards only for clearly relevant support.\n"
            "- Do not cite tangential cards just because they were retrieved.\n"
            "- Do not invent numeric claims, percentages, benchmark deltas, or implementation details unless they appear in the provided Wiki/Web passages.\n"
            "- For paper questions, answer the paper's specific contribution first before discussing downstream applications.\n\n"
            f"Tool Plan:\n{tool_text}\n\n"
            f"Tool Observations:\n{observation_text}\n\n"
            "Legacy local instruction block below may contain encoding-damaged text; follow the English tool policy above first.\n"
        )
        return (
            tool_policy +
            "你是用户的私人 Wiki 助手，但不是只能复述 Wiki。\n"
            "回答优先级：1) 优先使用强相关 Wiki 笔记；2) Wiki 不足时，可以使用你自己的通用知识直接回答；"
            "3) 若提供了 Web Search 结果，可作为临时外部参考；4) 不要把弱相关 Wiki 笔记硬凑成依据。\n"
            "回答要求：中文、结论先行、结构清楚。引用 Wiki 时使用 [1] [2]；"
            "引用 Web 时使用 [W1] [W2]，并在末尾列出对应标题和 URL。"
            "如果 Wiki 不足，先给出可用答案，再输出一个「推荐补充资料」小节，"
            "从候选学习资源里挑论文、视频、面经/博客各 1-2 个。\n\n"
            f"用户画像：\n{profile_text}\n\n"
            f"相关记忆：\n{memory_text}\n\n"
            f"最近对话：\n{history_text or '(无)'}\n\n"
            f"用户问题：{message}\n\n"
            f"本轮检索查询：{effective_query or message}\n\n"
            f"强相关 Wiki 笔记：\n{chr(10).join(context_blocks) if context_blocks else '(无强相关 Wiki 笔记)'}\n\n"
            f"Web Search 临时参考：\n{web_text}\n\n"
            f"候选学习资源：\n{resource_text}\n"
        )

    def _update_profile_from_message(
        self,
        message: str,
        answer: str,
        cards: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        if not self.learning_profile:
            return []

        updates: List[Dict[str, str]] = []
        extracted = self.profile_extractor.extract(message, cards)

        for pref in extracted["preferences"]:
            if self.session_store:
                self.session_store.upsert_preference(
                    pref["key"],
                    pref["value"],
                    evidence=message[:120],
                )
                updates.append({"signal_type": "preference", "value": f'{pref["key"]}={pref["value"]}'})

        for signal in extracted["signals"]:
            self.learning_profile.upsert_signal(
                signal["signal_type"],
                signal["key"],
                signal["value"],
                weight=float(signal.get("weight", "1.0")),
                evidence=message[:120],
                source="wiki_chat",
            )
            updates.append({"signal_type": signal["signal_type"], "value": signal["value"]})

        if self.session_store:
            for episode in extracted["episodes"]:
                self.session_store.add_episode(
                    topic=episode["topic"],
                    detail=episode["detail"],
                    paper=episode["paper"],
                    ttl_days=30,
                )

        for card in cards[:3]:
            self.learning_profile.log_event(
                "wiki_chat",
                topic=card.get("title", ""),
                detail=message[:240],
                metadata={"card_id": card.get("id"), "answer": answer[:300]},
            )
        return updates

    def _save_turn(
        self,
        session_id: str,
        message: str,
        answer: str,
        citations: List[WikiCitation],
        resources: List[Dict[str, str]],
        profile_updates: List[Dict[str, str]],
        tool_plan: Optional[Dict[str, Any]] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.session_store:
            return
        session_id = self.session_store.ensure_session(
            session_id,
            title=self._session_title_from_message(message),
            settings={"mode": "wiki_chat"},
        )
        try:
            self.session_store.save_message(session_id, "user", message, metadata={"mode": "wiki_chat"})
            self.session_store.save_message(
                session_id,
                "assistant",
                answer,
                metadata={
                    "mode": "wiki_chat",
                    "citations": [citation.__dict__ for citation in citations],
                    "resources": resources,
                    "profile_updates": profile_updates,
                    "tool_plan": tool_plan or {},
                    "trace": trace or {},
                },
            )
            session = self.session_store.get_session(session_id) or {}
            if not session.get("title") or session.get("title") == "新会话":
                self.session_store.update_session_title(session_id, self._session_title_from_message(message))
            self._archive_turn_if_useful(
                session_id=session_id,
                message=message,
                answer=answer,
                citations=citations,
                resources=resources,
                tool_plan=tool_plan or {},
                trace=trace or {},
            )
        except Exception as exc:
            print(f"[WikiChatService] save turn failed: {exc}")

    def _archive_turn_if_useful(
        self,
        *,
        session_id: str,
        message: str,
        answer: str,
        citations: List[WikiCitation],
        resources: List[Dict[str, str]],
        tool_plan: Dict[str, Any],
        trace: Dict[str, Any],
    ) -> None:
        try:
            archive = QueryArchive(db_path=getattr(self.session_store, "db_path", None))
            if not archive.should_archive(
                question=message,
                answer=answer,
                citations=citations,
                resources=resources,
                trace=trace,
            ):
                return
            archive.archive_turn(
                session_id=session_id,
                question=message,
                answer=answer,
                citations=citations,
                resources=resources,
                tool_plan=tool_plan,
                trace=trace,
            )
        except Exception as exc:
            print(f"[WikiChatService] query archive failed: {exc}")

    def _load_history(self, session_id: str) -> List:
        if not self.session_store or not session_id:
            return []
        try:
            return self.session_store.get_history(session_id, last_n=6)
        except Exception:
            return []

    def _effective_query(self, message: str, history: List) -> str:
        message = (message or "").strip()
        if not history or not self._looks_like_followup(message):
            return message

        recent_parts: List[str] = []
        for user_text, assistant_text in history[-2:]:
            if user_text:
                recent_parts.append(f"上一问题：{user_text}")
            if assistant_text:
                compact = " ".join(str(assistant_text).split())
                recent_parts.append(f"上一回答摘要：{compact[:220]}")

        context = " | ".join(recent_parts)
        return f"{context} | 当前追问：{message}"

    @staticmethod
    def _looks_like_followup(message: str) -> bool:
        text = (message or "").strip().lower()
        if len(text) <= 28:
            return True
        followup_markers = [
            "举例", "例子", "各个", "分别", "继续", "展开", "详细", "这个",
            "那个", "它", "他们", "上述", "前面", "刚才", "面试官", "怎么回答",
            "example", "examples", "elaborate", "continue",
        ]
        return any(marker in text for marker in followup_markers)

    def _should_use_external_tools(
        self,
        message: str,
        effective_query: str,
        cards: List[Dict[str, Any]],
    ) -> bool:
        if cards and not self._should_search_web(effective_query):
            return False
        if effective_query != message and self._is_example_followup(message):
            return False
        return self._should_search_web(effective_query) or not cards

    @staticmethod
    def _is_example_followup(message: str) -> bool:
        text = (message or "").strip().lower()
        markers = ["举例", "例子", "案例", "各个", "分别", "参考", "example", "examples"]
        return any(marker in text for marker in markers)

    def _filter_relevant_chunks(self, message: str, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not chunks:
            return []
        terms = self._meaningful_terms(message)
        if not terms:
            return chunks[:6]
        specific_terms = self._specific_terms(terms)

        scored = []
        for chunk in chunks:
            text = " ".join([
                str(chunk.get("title", "")),
                str(chunk.get("section", "")),
                str(chunk.get("text", "")),
            ]).lower()
            title = str(chunk.get("title", "")).lower()
            if specific_terms and not any(term.lower() in text for term in specific_terms):
                continue
            score = 0.0
            for term in terms:
                term_lower = term.lower()
                if term_lower in title:
                    score += 2.0
                if term_lower in text:
                    score += 1.0
            if score >= self._relevance_threshold(terms):
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored]

    def _filter_relevant_cards(self, message: str, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not cards:
            return []
        terms = self._meaningful_terms(message)
        if not terms:
            return cards
        specific_terms = self._specific_terms(terms)

        scored = []
        for card in cards:
            title = str(card.get("title", "")).lower()
            text = self._card_text(card).lower()
            if specific_terms and not any(term.lower() in text for term in specific_terms):
                continue
            score = 0.0
            for term in terms:
                term_lower = term.lower()
                if term_lower in title:
                    score += 2.0
                if term_lower in text:
                    score += 1.0
            if score >= self._relevance_threshold(terms):
                scored.append((score, card))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [card for _, card in scored]

    @classmethod
    def _meaningful_terms(cls, text: str) -> List[str]:
        terms = [term for term in cls._terms(text) if cls._is_meaningful_term(term)]
        return list(dict.fromkeys(terms))[:8]

    @staticmethod
    def _specific_terms(terms: List[str]) -> List[str]:
        generic = {"agent", "agents", "ai", "llm", "llms", "system", "systems", "大模型", "智能体"}
        return [term for term in terms if term.lower() not in generic]

    @staticmethod
    def _is_meaningful_term(term: str) -> bool:
        value = (term or "").strip().lower()
        if not value:
            return False
        stop = {
            "and", "or", "the", "for", "with", "from", "what", "how", "why",
            "are", "is", "was", "were", "this", "that", "into", "about",
            "什么", "怎么", "如何", "为什么", "区别", "不同", "对比", "比较",
            "主流", "技术", "里面", "这个", "那个", "一下", "哪些", "相关",
            "内容", "最近", "保存", "记录", "帮我", "总结",
        }
        return value not in stop

    @staticmethod
    def _relevance_threshold(terms: List[str]) -> float:
        if len(terms) <= 1:
            return 1.0
        if len(terms) <= 3:
            return 2.0
        return 2.5

    @staticmethod
    def _should_search_web(message: str) -> bool:
        text = (message or "").lower()
        private_scope = [
            "\u6211\u5e93\u91cc", "\u6211\u7684\u5e93", "\u6211\u7684wiki", "\u6211\u7684 wiki",
            "\u6211\u7684\u7b14\u8bb0", "\u77e5\u8bc6\u5e93", "my wiki", "my notes", "private wiki",
        ]
        explicit_external = [
            "\u6700\u65b0", "\u6700\u8fd1", "\u73b0\u5728", "\u5f53\u524d", "\u8054\u7f51",
            "\u641c\u7d22", "\u67e5\u4e00\u4e0b", "\u627e\u4e00\u4e0b", "\u5f00\u6e90",
            "web", "search", "google", "github", "arxiv", "sota", "leaderboard",
        ]
        if any(marker in text for marker in private_scope) and not any(marker in text for marker in explicit_external):
            return False
        triggers = [
            "\u6700\u65b0", "\u6700\u8fd1", "\u73b0\u5728", "\u5f53\u524d",
            "\u8d8b\u52bf", "\u4e3b\u6d41", "\u6709\u54ea\u4e9b", "\u641c\u7d22",
            "\u8054\u7f51", "\u67e5\u4e00\u4e0b", "\u627e\u4e00\u4e0b", "\u8d44\u6599",
            "\u8bba\u6587", "\u5bf9\u6bd4", "\u533a\u522b", "\u5f00\u6e90",
            "benchmark", "leaderboard", "sota", "state of the art",
            "web", "search", "google", "paper", "papers", "github", "arxiv",
            "react", "plan and execute", "plan-and-execute",
        ]
        return any(trigger in text for trigger in triggers)

    @staticmethod
    def _should_recommend_resources(message: str) -> bool:
        text = (message or "").lower()
        triggers = [
            "\u8d44\u6599", "\u8bba\u6587", "\u63a8\u8350", "\u89c6\u9891", "\u6559\u7a0b",
            "\u535a\u5ba2", "\u94fe\u63a5", "\u53c2\u8003", "paper", "papers", "tutorial",
            "video", "course", "blog", "resource", "resources", "link", "github", "arxiv",
        ]
        return any(trigger in text for trigger in triggers)

    @staticmethod
    def _web_search_query(message: str) -> str:
        text = (message or "").lower()
        if ("agent" in text or "\u667a\u80fd\u4f53" in text) and ("\u6846\u67b6" in text or "framework" in text):
            return "AI Agent frameworks LangChain AutoGen CrewAI Semantic Kernel"
        if "react" in text and "plan" in text and "execute" in text:
            return "ReAct vs Plan-and-Execute AI agent pattern difference"
        if "react" in text and ("agent" in text or "\u667a\u80fd\u4f53" in text):
            return "ReAct AI agent reasoning acting framework"
        return message

    @staticmethod
    def _web_context(web_results: List[Any]) -> str:
        if not web_results:
            return "(无)"
        lines = []
        for index, item in enumerate(web_results[:5], start=1):
            title = getattr(item, "title", "")
            url = getattr(item, "url", "")
            snippet = getattr(item, "snippet", "")
            site = getattr(item, "site", "")
            published_at = getattr(item, "published_at", "")
            author = getattr(item, "author", "")
            text_excerpt = getattr(item, "text_excerpt", "")
            passages = getattr(item, "passages", None) or []
            parts = [f"[W{index}] {title}", f"url: {url}"]
            if site:
                parts.append(f"site: {site}")
            if published_at:
                parts.append(f"published_at: {published_at}")
            if author:
                parts.append(f"author: {author}")
            if snippet:
                parts.append(f"snippet: {snippet}")
            for passage_index, passage in enumerate(passages[:3], start=1):
                if isinstance(passage, dict) and passage.get("text"):
                    parts.append(f"passage {passage_index}: {str(passage.get('text'))[:900]}")
            if text_excerpt and not passages:
                parts.append(f"excerpt: {str(text_excerpt)[:900]}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines)

    @staticmethod
    def _resource_context(resources: List[Dict[str, str]]) -> str:
        if not resources:
            return "(无)"
        labels = {
            "paper": "论文",
            "video": "视频",
            "interview_post": "面经/博客",
        }
        lines = []
        for index, item in enumerate(resources[:8], start=1):
            category = labels.get(item.get("category", ""), item.get("category", "资料"))
            lines.append(
                f"[R{index}] category: {category}\n"
                f"title: {item.get('title', '')}\n"
                f"url: {item.get('url', '')}\n"
                f"snippet: {item.get('snippet', '')}"
            )
        return "\n\n".join(lines)

    @staticmethod
    def _citation(card: Dict[str, Any]) -> WikiCitation:
        return WikiCitation(
            card_id=card.get("id", ""),
            title=card.get("title", ""),
            page_type=card.get("page_type", ""),
            summary=card.get("summary", ""),
            markdown_path=card.get("markdown_path", ""),
        )

    @staticmethod
    def _dedupe_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        result = []
        for card in cards:
            urls = card.get("source_urls") or []
            key = (
                str(card.get("title", "")).strip().lower(),
                str(urls[0] if urls else card.get("markdown_path", "")).strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(card)
        return result

    @staticmethod
    def _card_text(card: Dict[str, Any]) -> str:
        return " ".join([
            str(card.get("title", "")),
            str(card.get("summary", "")),
            str(card.get("content_json", "")),
            " ".join(card.get("related_topics", []) or []),
        ])

    @staticmethod
    def _compact_content(content: Dict[str, Any]) -> str:
        parts = []
        for key, value in (content or {}).items():
            if value not in ("", None, [], {}):
                parts.append(f"{key}: {value}")
        return "\n".join(parts)[:1600]

    @staticmethod
    def _terms(text: str) -> List[str]:
        terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{1,}|[\u4e00-\u9fff]{2,}", text or "")
        return list(dict.fromkeys(terms))

    @classmethod
    def _extract_topics(cls, text: str) -> List[str]:
        candidates = cls._terms(text)
        stop = {"这个", "那个", "什么", "怎么", "如何", "为什么", "哪些", "一下", "帮我", "总结"}
        result = []
        for item in candidates:
            if item.lower() in stop or item in stop:
                continue
            if len(item) > 24:
                continue
            result.append(item)
        return result[:8]

    def _profile_context(self) -> str:
        if not self.learning_profile:
            return "(无)"

        lines: List[str] = []
        goals = self.learning_profile.get_goals()
        if goals:
            lines.append("goals: " + " | ".join(goals[:3]))

        signals = self.learning_profile.get_profile_signals(limit=12)
        grouped: Dict[str, List[str]] = {}
        for item in signals:
            grouped.setdefault(item["signal_type"], []).append(str(item["value"]))
        for key in ("interest", "weak_point", "preference"):
            if grouped.get(key):
                lines.append(f"{key}: " + " | ".join(grouped[key][:5]))

        if self.session_store:
            prefs = self.session_store.get_all_preferences()
            stable = []
            for key in ("language_preference", "answer_length", "answer_style", "citation_preference", "explanation_style"):
                if prefs.get(key):
                    stable.append(f"{key}={prefs[key]}")
            if stable:
                lines.append("stable_preferences: " + " | ".join(stable))

        return "\n".join(lines) if lines else "(无)"

    def _episode_context(self, message: str) -> str:
        if not self.session_store:
            return "(无)"
        try:
            episodes = self.session_store.search_episodes(message, limit=4)
        except Exception:
            episodes = []
        if not episodes:
            return "(无)"
        lines = []
        for item in episodes:
            if not isinstance(item, dict):
                item = dict(item)
            topic = item.get("topic", "")
            detail = item.get("detail", "")
            paper = item.get("paper", "")
            if paper:
                lines.append(f"- {topic} | paper={paper} | detail={detail}")
            else:
                lines.append(f"- {topic} | detail={detail}")
        return "\n".join(lines)

    @staticmethod
    def _session_title_from_message(message: str) -> str:
        text = " ".join((message or "").split()).strip()
        if not text:
            return "新会话"
        return text[:24]
