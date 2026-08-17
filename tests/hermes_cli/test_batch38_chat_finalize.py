"""批三十八：会话生命周期显式状态机 —— web chat turn finalize 统一入口。

任务 1（status 字段落库）+ 任务 2（重试耗尽/异常 → ended(error)，绝不静默退出）
+ 任务 3（stop → interrupted + busy 复位）+ 任务 4（审批超时/拒绝 → ended(
approval_timeout/denied)）。agent 用带真 SessionDB 的 stub（不触发真 LLM），
硬约束断言落到 state.db 行状态 + SSE 事件 + busy 复位后可继续。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server
from hermes_state import SessionDB


class _FinalizeStubAgent:
    """带真 SessionDB 的 agent stub：turn 行为可注入（结果/异常/审批回调）。"""

    def __init__(self, session_id: str, session_db, *, result=None, block_event=None,
                 raise_exc=None, run_approval=False):
        self.session_id = session_id
        self._session_db = session_db
        self.result = result or {
            "final_response": "Hello World",
            "api_calls": 1,
            "completed": True,
            "partial": False,
            "interrupted": False,
            "failed": False,
        }
        self.block_event = block_event
        self.raise_exc = raise_exc
        self.run_approval = run_approval
        self.approval_result = None
        self.interrupt_calls = []
        self._interrupt_requested = False

    def run_conversation(self, message, **kwargs):
        if self.block_event is not None:
            self.block_event.wait(timeout=30)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.run_approval:
            from tools.terminal_tool import _get_approval_callback
            cb = _get_approval_callback()
            self.approval_result = cb("echo hi", "test approval") if cb else None
        result = dict(self.result)
        if self._interrupt_requested:
            result["interrupted"] = True
        return result

    def interrupt(self, message=None, *, hard_cancel=False):
        self.interrupt_calls.append((message, hard_cancel))
        self._interrupt_requested = True
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

    def _install(**kwargs) -> _FinalizeStubAgent:
        db = SessionDB()
        agent = _FinalizeStubAgent("", db, **kwargs)
        holder["agent"] = agent
        holder["db"] = db
        def _fake_create(sid, model=None):
            # 生产路径 _create_chat_agent 会建行；stub 里等价物就是它。
            try:
                db.create_session(sid, source="web")
            except Exception:
                pass
            agent.session_id = sid
            return agent
        monkeypatch.setattr(chat_api, "_create_chat_agent", _fake_create)
        return agent

    return _install, holder


def _new_session(client) -> str:
    resp = client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_session_id"]


def _sse_events(client, url, json_body=None, timeout=30):
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


def _row(db, sid):
    row = db.get_session(sid)
    assert row is not None
    return row


def test_normal_turn_ends_with_ended_status_and_busy_reset(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install()
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    assert any(ev == "chat:done" for ev, _ in events)
    row = _row(holder["db"], sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "turn_complete"
    assert row["ended_at"] is not None
    assert chat_api._CHAT_SESSIONS[sid].busy is False
    # 下一轮 turn 起点把行状态复位 running（阻塞 turn 进行中观察）。
    agent.block_event = threading.Event()

    def _consume():
        with client.stream(
            "POST", f"/api/chat/sessions/{sid}/messages", json={"message": "again"}
        ) as resp:
            for _ in resp.iter_lines():
                pass

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    for _ in range(300):
        if chat_api._CHAT_SESSIONS[sid].busy:
            break
        time.sleep(0.02)
    assert _row(holder["db"], sid)["status"] == "running"
    agent.block_event.set()
    t.join(timeout=10)
    assert _row(holder["db"], sid)["status"] == "ended"


def test_error_result_finalizes_ended_error(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    _install(result={
        "final_response": "",
        "api_calls": 3,
        "completed": False,
        "failed": True,
        "error": "API call failed after 3 retries: HTTP 500",
    })
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    assert any(ev == "chat:error" for ev, _ in events)
    row = _row(holder["db"], sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "error"
    assert row["ended_at"] is not None
    assert chat_api._CHAT_SESSIONS[sid].busy is False


def test_exception_finalizes_ended_error_not_silent(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    _install(raise_exc=RuntimeError("worker exploded"))
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    assert any(ev == "chat:error" for ev, _ in events)
    row = _row(holder["db"], sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "error"
    assert chat_api._CHAT_SESSIONS[sid].busy is False


def test_interrupt_finalizes_interrupted_and_allows_continue(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install(block_event=threading.Event())
    sid = _new_session(client)

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
        time.sleep(0.02)
    assert chat_api._CHAT_SESSIONS[sid].busy is True

    resp = client.post(f"/api/chat/sessions/{sid}/interrupt")
    assert resp.status_code == 200, resp.text
    t.join(timeout=10)
    assert agent.interrupt_calls and agent.interrupt_calls[-1][1] is True
    assert chat_api._CHAT_SESSIONS[sid].busy is False
    row = _row(holder["db"], sid)
    assert row["status"] == "interrupted"
    assert row["end_reason"] == "interrupted"
    # 中断后 busy 已复位 → 再输入可用（不再 409）。
    resp = client.post(
        f"/api/chat/sessions/{sid}/messages", json={"message": "hello again"}
    )
    assert resp.status_code == 200, resp.text
    with client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages", json={"message": "hello again"}
    ) as stream:
        assert stream.status_code == 200


def test_approval_timeout_finalizes_ended_approval_timeout(client, stub_agent_factory, tmp_path, monkeypatch):
    """§AW：审批超时后 turn 落 ended(approval_timeout)，UI 可继续输入。"""
    import hermes_cli.config as hc

    _install, holder = stub_agent_factory
    _install(run_approval=True)
    sid = _new_session(client)
    # 缩短审批超时到 1s 并清配置缓存，让会话层自限等待窗口生效。
    (tmp_path / "config.yaml").write_text(
        "approvals:\n  mode: manual\n  timeout: 1\n",
        encoding="utf-8",
    )
    hc._LOAD_CONFIG_CACHE.clear()
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"}, timeout=30)
    assert any(ev == "chat:approval_pending" for ev, _ in events)
    assert any(ev in ("chat:done", "chat:error") for ev, _ in events)
    row = _row(holder["db"], sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "approval_timeout"
    assert chat_api._CHAT_SESSIONS[sid].busy is False


def test_approval_denied_finalizes_ended_denied(client, stub_agent_factory):
    from tools.approval import (
        deny_web_approval,
        get_web_approval,
        list_web_approvals,
    )

    _install, holder = stub_agent_factory
    _install(run_approval=True)
    sid = _new_session(client)

    def _consume():
        with client.stream(
            "POST", f"/api/chat/sessions/{sid}/messages", json={"message": "hi"}
        ) as resp:
            for _ in resp.iter_lines():
                pass

    t = threading.Thread(target=_consume, daemon=True)
    t.start()
    approval_id = None
    for _ in range(300):
        pending, _total = list_web_approvals(status="pending")
        if pending:
            approval_id = pending[0]["id"]
            break
        time.sleep(0.02)
    assert approval_id is not None
    assert deny_web_approval(approval_id)["status"] == "denied"
    t.join(timeout=10)
    row = _row(holder["db"], sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "denied"
    assert chat_api._CHAT_SESSIONS[sid].busy is False
    assert get_web_approval(approval_id)["status"] == "denied"


def test_finalize_failure_stamps_finalize_error(client, stub_agent_factory, monkeypatch):
    """finalize 自身失败 → 行落 finalize_error（可查），不允许静默跳过。"""
    _install, holder = stub_agent_factory
    _install()
    sid = _new_session(client)
    db = holder["db"]
    real_write = db._execute_write
    calls = []

    def flaky(fn, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            # 第 1 次写是 turn 起点复位 running，第 2 次才是 finalize 主写。
            raise RuntimeError("db wedged")
        return real_write(fn, **kwargs)

    monkeypatch.setattr(db, "_execute_write", flaky)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    assert any(ev == "chat:done" for ev, _ in events)
    row = _row(db, sid)
    assert row["status"] == "finalize_error"
    assert chat_api._CHAT_SESSIONS[sid].busy is False
