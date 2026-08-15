from system.wiki.wiki_chat import ToolCallPlan, WikiChatResult, WikiChatService, WikiToolPlan


class _StreamingWikiChatService(WikiChatService):
    def __init__(self):
        super().__init__(wiki_store=object(), wiki_resolver=object())

    def _load_history(self, session_id):
        return []

    def _effective_query(self, message, history):
        return message

    def _run_tool_loop(self, message, effective_query, history, limit=6, max_steps=3, event_callback=None):
        running = {
            "type": "tool_status",
            "event_id": "wiki_search:test query",
            "tool": "wiki_search",
            "label": "Wiki Search",
            "status": "running",
            "detail": "resolve Wiki pages",
            "query": "test query",
            "items": [],
        }
        done = {
            **running,
            "status": "done",
            "detail": "resolved 1 compiled Wiki page",
            "items": [{"card_id": "card-1", "title": "Test Page", "page_type": "PaperPage"}],
        }
        if event_callback:
            event_callback(running)
            event_callback(done)

        plan = WikiToolPlan(tools=[ToolCallPlan("wiki_search", "test query", "resolve Wiki pages")])
        observation = {
            "tool": "wiki_search",
            "query": "test query",
            "status": "done",
            "summary": "resolved 1 compiled Wiki page",
            "items": done["items"],
        }
        return {
            "plan": plan,
            "cards": [],
            "web_results": [],
            "resources": [],
            "trace": {
                "tool_plan": self._plan_payload(plan),
                "tool_observations": [observation],
                "retrieved_cards": [],
                "web_results": [],
                "resources": [],
                "diagnostics": {"wiki_card_count": 0, "web_result_count": 0, "resource_count": 0},
            },
            "events": [done],
        }

    def _answer(self, *args, **kwargs):
        return "test answer"

    def _update_profile_from_message(self, *args, **kwargs):
        return []

    def _save_turn(self, *args, **kwargs):
        return None


def test_chat_stream_emits_running_before_done():
    service = _StreamingWikiChatService()

    chunks = list(service.chat_stream("test question"))
    tool_chunks = [chunk for chunk in chunks if chunk.get("type") == "tool_status"]

    assert [chunk["status"] for chunk in tool_chunks] == ["running", "done"]
    assert tool_chunks[0]["event_id"] == tool_chunks[1]["event_id"]
    assert tool_chunks[1]["items"][0]["title"] == "Test Page"


def test_sync_chat_remains_a_regular_result():
    service = _StreamingWikiChatService()

    result = service.chat("test question")

    assert isinstance(result, WikiChatResult)
    assert result.answer == "test answer"
