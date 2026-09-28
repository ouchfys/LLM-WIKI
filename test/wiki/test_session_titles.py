from types import SimpleNamespace

import pytest

from system.agent_runtime import AgentRunStore
from system.agent_runtime.control import RunControl
from system.conversation.session_store import SessionStore
from system.wiki.wiki_chat import WikiChatService


def test_empty_sessions_are_distinguished_from_interrupted_tasks_and_legacy_titles(tmp_path):
    store = SessionStore(str(tmp_path / 'sessions.db'))
    empty = store.create_session('新对话')
    old = store.create_session('新对话')
    store.save_message(old, 'user', '解释 Agent 自我改进')
    running = store.create_session('新对话')
    AgentRunStore(store.db_path).create_run(run_type='wiki_chat', source_uri=f'session:{running}')
    items = {item['id']: item for item in store.list_sessions()}
    assert items[empty]['has_activity'] is False
    assert items[old]['has_activity'] is True
    assert items[old]['title'] == '解释 Agent 自我改进'
    assert items[running]['has_activity'] is True


@pytest.mark.parametrize('output', ['RSI 论文发展路线', '', '标题\n多余解释', 'x' * 41, RuntimeError('quota')])
def test_title_uses_only_first_prompt_once_and_falls_back_without_blocking(tmp_path, output):
    store = SessionStore(str(tmp_path / 'sessions.db'))
    sid = store.create_session('新对话')
    first = '请梳理这11篇RSI论文，按照框架、方法和评测整理发展路线'
    request = store.begin_session_title(sid, first)
    assert store.begin_session_title(sid, '哈登还是库里') == request
    prompts = []

    def invoke(prompt, **kwargs):
        prompts.append(prompt)
        if isinstance(output, Exception):
            raise output
        return output

    service = WikiChatService(object(), wiki_resolver=object(), session_store=store,
                              title_llm=SimpleNamespace(invoke=invoke))
    control = RunControl(None, '', '哈登还是库里')
    control.session_id, control.title_request = sid, request
    events = []
    with control.bind():
        service._generate_session_title(control, events.append)
        service._generate_session_title(control, events.append)
    assert len(prompts) == 1
    assert first in prompts[0] and '哈登' not in prompts[0]
    assert store.get_session(sid)['title'] == (output if output == 'RSI 论文发展路线' else first[:24])
    assert events[0]['type'] == 'session_updated'
    assert store.begin_session_title(sid, 'later prompt') is None


def test_title_response_and_legacy_chat_save_cannot_resurrect_deleted_session(tmp_path):
    store = SessionStore(str(tmp_path / 'sessions.db'))
    sid = store.create_session('新对话')
    request = store.begin_session_title(sid, 'First question')
    store.delete_session(sid)
    assert not store.finish_session_title(sid, request, 'Late title')
    service = WikiChatService(object(), wiki_resolver=object(), session_store=store)
    assert service._save_turn(sid, 'First question', 'Late answer', [], [], []) == []
    assert store.get_session(sid) is None
    assert store.list_sessions() == []


def test_manual_title_and_earlier_user_message_take_precedence(tmp_path):
    store = SessionStore(str(tmp_path / 'sessions.db'))
    sid = store.create_session('新对话')
    store.save_message(sid, 'user', 'The actual first prompt')
    request = store.begin_session_title(sid, 'Later instruction')
    assert request['source'] == 'The actual first prompt'
    store.update_session_title(sid, 'My chosen name')
    assert not store.finish_session_title(sid, request, 'Model title')
    assert store.get_session(sid)['title'] == 'My chosen name'
