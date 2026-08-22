"""批三十一：对话 Session API（/api/chat/*，契约 ~/notes/vigil-batch31-prompt.md）。

分层：只打 chat_api 新增端点 + 审批联动，agent 用 stub（不触发真 LLM/工具）。
硬约束断言：busy 串行 409；未知会话 404；SSE 事件名照批二十八风格
（chat:delta / chat:tool / chat:tool_result / chat:approval_pending /
chat:done / chat:error）；审批展示文本过 redact（凭据零泄露）。
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _StubAgent:
    """可脚本化的 agent stub：turn 行为可注入（block_event 阻塞直到放行）。"""

    _session_db = None

    def __init__(self, block_event: threading.Event = None):
        self.calls = []
        self.block_event = block_event

    def run_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        if self.block_event is not None:
            self.block_event.wait(timeout=30)
        sc = kwargs.get("stream_callback")
        tc = kwargs.get("tool_callback")
        if sc:
            sc("Hello ")
        if tc:
            tc({"type": "tool_start", "name": "read_file", "input_summary": "read /etc/hostname"})
        if tc:
            tc({"type": "tool_end", "name": "read_file", "output_summary": "ok: hostname", "ok": True})
        if sc:
            sc("World")
        return {"final_response": "Hello World", "api_calls": 1, "completed": True, "partial": False}


@pytest.fixture()
def client(monkeypatch):
    """TestClient + 隔离 VIGIL_HOME + stub agent 工厂 + 注册表清理。"""
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


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    from tools.approval import clear_web_approvals
    import hermes_cli.config as hc

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    clear_web_approvals()
    (tmp_path / "config.yaml").write_text(
        "approvals:\n  mode: manual\n  timeout: 30\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    clear_web_approvals()


@pytest.fixture()
def stub_agent_factory(monkeypatch):
    holder: dict = {}

    def _install(block_event: threading.Event = None) -> _StubAgent:
        agent = _StubAgent(block_event=block_event)
        holder["agent"] = agent
        monkeypatch.setattr(chat_api, "_create_chat_agent", lambda sid, model=None, provider=None: agent)
        return agent

    return _install, holder


def _new_session(client) -> str:
    resp = client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_session_id"]


def _sse_events(client, url, json_body=None):
    events = []
    with client.stream("POST", url, json=json_body) as resp:
        assert resp.status_code == 200, resp.text
        ev = None
        data_lines = []
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and ev is not None:
                events.append((ev, json.loads("\n".join(data_lines))))
                ev = None
                data_lines = []
    return events


def test_create_and_list_session(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    _install()
    sid = _new_session(client)
    assert sid.startswith("chat_")
    assert holder["agent"] is not None

    resp = client.get("/api/chat/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["sessions"][0]["id"] == sid
    assert body["sessions"][0]["busy"] is False


def test_send_message_streams_events(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    _install()
    sid = _new_session(client)
    events = _sse_events(
        client,
        f"/api/chat/sessions/{sid}/messages",
        json_body={"message": "看下拓扑"},
    )
    types = [ev[0] for ev in events]
    assert "chat:delta" in types
    assert "chat:tool" in types
    assert "chat:tool_result" in types
    assert "chat:done" in types
    done = [ev[1] for ev in events if ev[0] == "chat:done"][0]
    assert done["final_response"] == "Hello World"
    assert done["api_calls"] == 1
    # turn 完成 → busy 复位
    assert chat_api._CHAT_SESSIONS[sid].busy is False
    # 上下文延续：agent 收到 conversation_history（首轮为空列表）
    msg, kwargs = holder["agent"].calls[0]
    assert msg == "看下拓扑"
    assert kwargs["conversation_history"] == []
    assert kwargs["task_id"] == sid
    assert callable(kwargs["stream_callback"])
    assert callable(kwargs["tool_callback"])


def test_busy_returns_409_and_recovers_after_done(client, stub_agent_factory):
    block = threading.Event()
    _install, holder = stub_agent_factory
    _install(block_event=block)
    sid = _new_session(client)

    result: dict = {}

    def _consume():
        with client.stream(
            "POST", f"/api/chat/sessions/{sid}/messages", json={"message": "hi"}
        ) as resp:
            result["status"] = resp.status_code
            for _ in resp.iter_lines():
                pass

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    # 等 worker 进入 stub（busy=True）
    for _ in range(200):
        if chat_api._CHAT_SESSIONS[sid].busy:
            break
        threading.Event().wait(0.02)
    assert chat_api._CHAT_SESSIONS[sid].busy is True

    resp = client.post(f"/api/chat/sessions/{sid}/messages", json={"message": "again"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "busy"

    block.set()
    t.join(timeout=15)
    assert not t.is_alive()
    assert chat_api._CHAT_SESSIONS[sid].busy is False
    # 释放后可继续发消息
    resp = client.post(f"/api/chat/sessions/{sid}/messages", json={"message": "again"})
    assert resp.status_code == 200


def test_unknown_session_404_and_empty_message_400(client, stub_agent_factory):
    _install, _ = stub_agent_factory
    _install()
    resp = client.post("/api/chat/sessions/chat_nope/messages", json={"message": "hi"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"

    sid = _new_session(client)
    resp = client.post(f"/api/chat/sessions/{sid}/messages", json={"message": "   "})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_approval_callback_registers_web_approval_and_waits(monkeypatch):
    """审批联动：chat:approval_pending 事件 + 注册表条目 + approve 放行回调。"""
    from tools.approval import approve_web_approval, get_web_approval

    async def _main():
        session = chat_api.ChatSession(
            chat_session_id="chat_approval", agent=_StubAgent(), session_db=None
        )
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cb = chat_api._approval_callback_factory(session, queue, loop)
        result_box: dict = {}

        def _run_cb():
            result_box["choice"] = cb(
                "kubectl -n prod rollout restart deploy/gateway-svc --token=hunter2sec",
                "prod 变更确认门（B'）",
                allow_session=False,
                allow_permanent=False,
            )

        t = threading.Thread(target=_run_cb, daemon=True)
        t.start()
        for _ in range(200):
            if queue.qsize() > 0:
                break
            await asyncio.sleep(0.02)
        kind, ev = await queue.get()
        assert kind == "event"
        assert ev["type"] == "chat:approval_pending"
        assert isinstance(ev["env"], str)
        assert ev["grade"] is None or isinstance(ev["grade"], str)
        # redact：凭据不进审批展示
        assert "hunter2sec" not in ev["command"]
        av = get_web_approval(ev["approval_id"])
        assert av is not None and av["status"] == "pending"
        assert "hunter2sec" not in json.dumps(av, ensure_ascii=False)

        r = approve_web_approval(ev["approval_id"], scope="once")
        assert r["status"] == "approved"
        t.join(timeout=5)
        assert not t.is_alive()
        assert result_box["choice"] == "once"

    asyncio.run(_main())
