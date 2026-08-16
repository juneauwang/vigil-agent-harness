"""批三十六：对话页"停止"——/api/chat/sessions/{id}/interrupt 端点。

分层照批三十一：只打 interrupt 端点 + 审批取消联动，agent 用 stub（不触发
真 LLM）。硬约束断言：未知会话 404；不 busy 409（幂等）；中断后 busy 复位
可继续发消息；chat:done 带 interrupted 标记（事件类型零新增）；挂起审批被
取消（denied，审批中心不再 pending）。
"""

from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _InterruptibleStubAgent:
    """可脚本化的 agent stub：run_conversation 阻塞直到放行/被 interrupt。"""

    _session_db = None

    def __init__(self, block_event: threading.Event = None,
                 result: dict = None):
        self.block_event = block_event
        self.result = result or {
            "final_response": "Hello World",
            "api_calls": 1,
            "completed": True,
            "partial": False,
            "interrupted": False,
            "failed": False,
        }
        self.interrupt_calls = []

    def run_conversation(self, message, **kwargs):
        if self.block_event is not None:
            self.block_event.wait(timeout=30)
        return self.result

    def interrupt(self, message=None, *, hard_cancel=False):
        self.interrupt_calls.append((message, hard_cancel))
        if self.block_event is not None:
            self.block_event.set()


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

    def _install(block_event: threading.Event = None,
                 result: dict = None) -> _InterruptibleStubAgent:
        agent = _InterruptibleStubAgent(block_event=block_event, result=result)
        holder["agent"] = agent
        monkeypatch.setattr(chat_api, "_create_chat_agent", lambda sid: agent)
        return agent

    return _install, holder


def _new_session(client) -> str:
    resp = client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_session_id"]


def _start_blocked_turn(client, sid, block_event) -> threading.Thread:
    """起一个阻塞中的 turn（worker 进入 stub 等待放行）。"""

    def _consume():
        with client.stream(
            "POST", f"/api/chat/sessions/{sid}/messages", json={"message": "hi"}
        ) as resp:
            for _ in resp.iter_lines():
                pass

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    for _ in range(300):
        if chat_api._CHAT_SESSIONS[sid].busy:
            break
        threading.Event().wait(0.02)
    assert chat_api._CHAT_SESSIONS[sid].busy is True
    return t


def test_interrupt_unknown_session_404(client, stub_agent_factory):
    _install, _ = stub_agent_factory
    _install()
    resp = client.post("/api/chat/sessions/chat_nope/interrupt")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_interrupt_not_busy_409(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install()
    sid = _new_session(client)
    # 空闲会话 → 409（幂等：重复中断/已收尾都是明确状态）
    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_busy"
    assert agent.interrupt_calls == []


def test_interrupt_success_unwinds_turn_and_allows_continue(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install(block_event=threading.Event())
    sid = _new_session(client)
    t = _start_blocked_turn(client, sid, agent.block_event)

    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "interrupted"
    assert body["chat_session_id"] == sid
    # 中断走 agent.interrupt(hard_cancel=True)（显式停止，非 redirect）
    assert agent.interrupt_calls == [(None, True)]
    # worker 收尾完成：busy 复位、turn 结束、可继续发消息
    t.join(timeout=10)
    assert not t.is_alive()
    assert chat_api._CHAT_SESSIONS[sid].busy is False
    resp = client.post(f"/api/chat/sessions/{sid}/messages", json={"message": "again"})
    assert resp.status_code == 200
    # 中断后再次 interrupt → 409 not_busy（幂等）
    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_busy"


def test_interrupt_done_event_carries_interrupted_flag(client, stub_agent_factory):
    """中断后的 SSE 收尾：chat:done 带 interrupted=true（事件类型零新增）。"""
    _install, holder = stub_agent_factory
    agent = _install(
        block_event=threading.Event(),
        result={
            "final_response": "已停止",
            "api_calls": 2,
            "completed": False,
            "partial": True,
            "interrupted": True,
            "failed": False,
        },
    )
    sid = _new_session(client)
    t = _start_blocked_turn(client, sid, agent.block_event)

    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 200
    t.join(timeout=10)
    assert not t.is_alive()

    events = []
    with client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages", json={"message": "again"}
    ) as resp:
        assert resp.status_code == 200
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
    done = [d for e, d in events if e == "chat:done"]
    assert done, "chat:done missing"
    assert done[0]["interrupted"] is True


def test_interrupt_cancels_pending_approval(client, stub_agent_factory):
    """审批挂起中中断 → 该审批被取消（denied），审批中心不再 pending。"""
    from tools.approval import (
        get_web_approval,
        list_web_approvals,
        register_web_approval,
    )

    _install, holder = stub_agent_factory
    agent = _install(block_event=threading.Event())
    sid = _new_session(client)

    approval_id = register_web_approval(
        command="rm -rf /tmp/vigil-b36-demo",
        description="delete in root path",
        env="test",
        session_key=sid,
        source="web",
    )

    t = _start_blocked_turn(client, sid, agent.block_event)
    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 200, resp.text
    assert resp.json()["approvals_cancelled"] == 1
    t.join(timeout=10)

    view = get_web_approval(approval_id)
    assert view is not None
    assert view["status"] == "denied"
    pending, _total = list_web_approvals(status="pending")
    assert approval_id not in [v["id"] for v in pending]


def test_interrupt_other_session_approval_left_pending(client, stub_agent_factory):
    """只取消本会话的挂起审批，其他会话的 pending 不受影响。"""
    from tools.approval import (
        get_web_approval,
        list_web_approvals,
        register_web_approval,
    )

    _install, holder = stub_agent_factory
    agent = _install(block_event=threading.Event())
    sid = _new_session(client)
    other = register_web_approval(
        command="chmod 777 /tmp/x",
        description="other session",
        env="test",
        session_key="chat_other",
        source="web",
    )

    t = _start_blocked_turn(client, sid, agent.block_event)
    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 200
    t.join(timeout=10)

    assert get_web_approval(other)["status"] == "pending"
    pending, _total = list_web_approvals(status="pending")
    assert other in [v["id"] for v in pending]
