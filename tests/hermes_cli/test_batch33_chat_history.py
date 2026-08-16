"""批三十三 T1a — 对话页会话历史端点（GET /api/chat/sessions/{id}/messages）。

覆盖：会话不存在 404；历史消息结构化（role/content/tools/timestamp）；
tool 调用 + 结果折叠进 assistant 气泡；内容过 redact（URL userinfo 掩码）；
busy 随注册表返回；session_meta 不过出。
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _FakeSessionDB:
    """可脚本化 session_db：返回预设的 OpenAI 消息 dict 列表。"""

    def __init__(self, history):
        self.history = history

    def get_messages_as_conversation(self, *args, **kwargs):
        return list(self.history)


@pytest.fixture()
def client(monkeypatch):
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    chat_api.clear_chat_sessions()
    try:
        yield test_client
    finally:
        chat_api.clear_chat_sessions()
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


@pytest.fixture()
def env_home(tmp_path, monkeypatch):
    import hermes_cli.config as hc

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _stub_agent_factory(monkeypatch):
    agent = object()
    monkeypatch.setattr(chat_api, "_create_chat_agent", lambda sid: agent)
    return agent


def _new_session(client, history=None, busy=False) -> str:
    resp = client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    sid = resp.json()["chat_session_id"]
    session = chat_api._CHAT_SESSIONS[sid]
    if history is not None:
        session.session_db = _FakeSessionDB(history)
    session.busy = busy
    return sid


def _sample_history():
    return [
        {"role": "user", "content": "看下拓扑", "_row_id": 1, "timestamp": 100.0},
        {"role": "assistant", "content": "", "_row_id": 2, "timestamp": 101.0,
         "tool_calls": [
             {"id": "call_1", "function": {"name": "terminal", "arguments": "kubectl get nodes"}},
         ]},
        {"role": "tool", "content": "node1 Ready\nnode2 NotReady", "_row_id": 3,
         "tool_name": "terminal", "tool_call_id": "call_1", "timestamp": 102.0},
        {"role": "assistant", "content": "共 2 台主机。", "_row_id": 4, "timestamp": 103.0},
        {"role": "session_meta", "content": "meta", "_row_id": 5},
    ]


def test_unknown_session_404(client):
    resp = client.get("/api/chat/sessions/chat_nope/messages")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_history_structured_and_tool_folding(client, monkeypatch):
    _stub_agent_factory(monkeypatch)
    sid = _new_session(client, history=_sample_history())
    resp = client.get(f"/api/chat/sessions/{sid}/messages")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chat_session_id"] == sid
    assert body["total"] == 3
    assert body["busy"] is False
    messages = body["messages"]

    # user 气泡
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "看下拓扑"
    assert messages[0]["tools"] == []
    assert messages[0]["timestamp"] == 100.0

    # assistant 工具行：tool_calls + 紧随 tool 结果折叠进同一气泡
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == ""
    assert len(messages[1]["tools"]) == 1
    tool = messages[1]["tools"][0]
    assert tool["name"] == "terminal"
    assert tool["input_summary"] == "kubectl get nodes"
    assert tool["output_summary"] == "node1 Ready node2 NotReady"
    assert tool["ok"] is True

    # 最终回复
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "共 2 台主机。"
    assert messages[2]["tools"] == []

    # session_meta 不过出
    names = [m["role"] for m in messages]
    assert "session_meta" not in names


def test_history_redacts_credentials(client, monkeypatch):
    _stub_agent_factory(monkeypatch)
    history = [
        {"role": "user", "content": "连 https://admin:secret123@example.com/api", "_row_id": 1},
        {"role": "assistant", "content": "done", "_row_id": 2},
    ]
    sid = _new_session(client, history=history)
    resp = client.get(f"/api/chat/sessions/{sid}/messages")
    assert resp.status_code == 200
    content = resp.json()["messages"][0]["content"]
    assert "secret123" not in content
    assert "admin" not in content
    assert "[已过滤]" in content


def test_history_busy_flag(client, monkeypatch):
    _stub_agent_factory(monkeypatch)
    sid = _new_session(client, history=[{"role": "user", "content": "hi", "_row_id": 1}], busy=True)
    resp = client.get(f"/api/chat/sessions/{sid}/messages")
    assert resp.status_code == 200
    assert resp.json()["busy"] is True


def test_history_empty_session(client, monkeypatch):
    _stub_agent_factory(monkeypatch)
    sid = _new_session(client, history=[])
    resp = client.get(f"/api/chat/sessions/{sid}/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []
    assert resp.json()["total"] == 0


def test_view_messages_pending_tool_without_output(monkeypatch):
    """工具结果缺失：输出摘要置空串，不崩。"""
    history = [
        {"role": "assistant", "content": "", "_row_id": 10,
         "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "/etc/hosts"}}]},
    ]
    view = chat_api._history_to_view_messages(history)
    assert len(view) == 1
    assert view[0]["tools"][0]["name"] == "read_file"
    assert view[0]["tools"][0]["output_summary"] == ""


def test_view_messages_orphan_tool_row(monkeypatch):
    """孤立 tool 行（无前置 assistant 工具调用）→ 独立 assistant 气泡。"""
    history = [
        {"role": "tool", "content": "stray output", "_row_id": 7, "tool_name": "terminal"},
    ]
    view = chat_api._history_to_view_messages(history)
    assert len(view) == 1
    assert view[0]["role"] == "assistant"
    assert view[0]["tools"][0]["name"] == "terminal"
    assert view[0]["tools"][0]["output_summary"] == "stray output"
