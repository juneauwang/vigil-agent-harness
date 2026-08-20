"""批四十九 — web chat clarify 交互（OPS-DELTA #66）。

LLM 调 clarify 此前在 web chat 无回调接线（chat_api 只有 _approval_callback_
factory）→ 拿到 "Clarify tool is not available"。本批新增：
  * _clarify_callback_factory —— 线程内回调：登记 session 级挂起条目 → SSE
    chat:clarify_pending → 阻塞等应答 → 返回选择串；
  * POST /api/chat/sessions/{id}/clarify —— 应答端点（404/409/400/200 语义）；
  * 超时 → "[user did not respond within Xm]"（agent 自行决定，复用既有
    clarify_timeout，不新增配置）；
  * 多 session 并行各自独立（状态挂在 ChatSession.pending_clarify 上）。

覆盖：SSE 事件字段、应答端点契约（含超时 409）、阻塞回调返回、多会话独立、
answer 不落日志。
"""

from __future__ import annotations

import queue as pyqueue
import threading
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _StubAgent:
    _session_db = None
    reasoning_callback = None
    clarify_callback = None

    def __init__(self, clarify_call=None):
        self.calls = []
        self.clarify_result = None
        self.clarify_call = clarify_call  # (question, choices, multi_select) 或不触发

    def run_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        if self.clarify_call:
            question, choices, multi = self.clarify_call
            if multi:
                self.clarify_result = self.clarify_callback(question, choices, multi_select=True)
            else:
                self.clarify_result = self.clarify_callback(question, choices)
        sc = kwargs.get("stream_callback")
        if sc:
            sc("最终答案")
        return {"final_response": "最终答案", "api_calls": 1, "completed": True, "partial": False}


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
    import hermes_cli.config as hc

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: anthropic/claude-opus-4.8\n  provider: openrouter\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture()
def stub_agent_factory(monkeypatch):
    holder: dict = {}

    def _install(**kwargs) -> _StubAgent:
        agent = _StubAgent(**kwargs)
        holder["agent"] = agent
        monkeypatch.setattr(chat_api, "_create_chat_agent", lambda sid, model=None: agent)
        return agent

    return _install, holder


def _new_session(client, model=None) -> str:
    body = {"model": model} if model else None
    resp = client.post("/api/chat/sessions", json=body) if body else client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_session_id"]


def _run_turn_in_thread(session, queue, loop):
    """起一个 turn（worker 线程驱动）；返回 (thread, 收集事件列表)。"""
    events: list = []
    stop = threading.Event()
    t_collect = threading.Thread(target=_collect_into, args=(queue, events, stop), daemon=True)
    t_collect.start()
    t_worker = threading.Thread(
        target=chat_api._run_chat_turn,
        args=(session, "hi", queue, loop),
        daemon=True,
        name=f"test-clarify-{session.chat_session_id[:8]}",
    )
    t_worker.start()
    return t_worker, t_collect, events, stop


class _SyncLoop:
    """最小 loop 替身：call_soon_threadsafe 同步直调（测试无需真实事件循环）。"""

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def _collect_into(q: pyqueue.Queue, target: list, stop: threading.Event):
    """后台收集线程：把 worker 推入 stdlib queue 的事件搬进 target 列表。"""
    while True:
        try:
            item = q.get(timeout=0.1)
        except pyqueue.Empty:
            if stop.is_set():
                return
            continue
        if item is None:
            return
        target.append(item)


def _wait_clarify(events, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in events:
            if item[0] == "event" and item[1].get("type") == "chat:clarify_pending":
                return item[1]
        time.sleep(0.02)
    raise AssertionError("未收到 chat:clarify_pending 事件")


def _wait_done(events, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in events:
            if item[0] == "event" and item[1].get("type") == "chat:done":
                return item[1]
        time.sleep(0.02)
    raise AssertionError("未收到 chat:done 事件")


def _wait_event_count(events, event_type: str, count: int, timeout=5.0):
    """等齐 count 个指定类型事件（多 session 并行断言各自到达）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = sum(1 for item in events if item[0] == "event" and item[1].get("type") == event_type)
        if n >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"未等齐 {count} 个 {event_type} 事件（当前 {n}）")


# ── 回调工厂 + SSE + 阻塞应答 全链路 ───────────────────────────────────────


def test_clarify_callback_full_flow(stub_agent_factory, monkeypatch):
    """LLM 调 clarify → SSE chat:clarify_pending → 应答（等价 POST /clarify）
    → 回调返回选择串 → 回合继续（chat:done）。"""
    _install, holder = stub_agent_factory
    agent = _install(clarify_call=("选哪个部署目标？", ["staging", "prod"], False))
    session = chat_api.ChatSession(
        chat_session_id="chat_test1",
        agent=agent,
        session_db=None,
    )
    chat_api._CHAT_SESSIONS[session.chat_session_id] = session
    loop = _SyncLoop()
    queue: pyqueue.Queue = pyqueue.Queue()

    try:
        t_worker, t_collect, events, stop = _run_turn_in_thread(session, queue, loop)
        ev = _wait_clarify(events)
        assert ev["type"] == "chat:clarify_pending"
        assert ev["session_id"] == session.chat_session_id
        assert ev["question"] == "选哪个部署目标？"
        assert ev["choices"] == ["staging", "prod"]
        assert ev["multi_select"] is False
        assert "timeout_at" in ev and ev["timeout_at"]
        assert session.pending_clarify is not None

        # 应答（等价前端 POST /api/chat/sessions/{id}/clarify）。
        entry = session.pending_clarify
        entry.response = "prod"
        entry.event.set()

        _wait_done(events)
        t_worker.join(timeout=5)
        stop.set()
        t_collect.join(timeout=2)
        assert agent.clarify_result == "prod"
        assert session.pending_clarify is None
    finally:
        chat_api.clear_chat_sessions()


def test_clarify_multi_select_answer_list(stub_agent_factory):
    """multi_select=True：回调返回列表（前端传数组，_parse_multi_select 解）。"""
    _install, holder = stub_agent_factory
    agent = _install(clarify_call=("要哪些环境？", ["staging", "prod"], True))
    session = chat_api.ChatSession(chat_session_id="chat_ms", agent=agent, session_db=None)
    chat_api._CHAT_SESSIONS[session.chat_session_id] = session
    loop = _SyncLoop()
    queue: pyqueue.Queue = pyqueue.Queue()
    try:
        t_worker, t_collect, events, stop = _run_turn_in_thread(session, queue, loop)
        ev = _wait_clarify(events)
        assert ev["multi_select"] is True
        entry = session.pending_clarify
        entry.response = ["staging", "prod"]
        entry.event.set()
        _wait_done(events)
        t_worker.join(timeout=5)
        stop.set()
        t_collect.join(timeout=2)
        assert agent.clarify_result == ["staging", "prod"]
    finally:
        chat_api.clear_chat_sessions()


def test_clarify_timeout_returns_sentinel(stub_agent_factory, monkeypatch):
    """超时（复用 clarify_timeout）→ 回调返回 "[user did not respond within Xm]"
    → agent 自行决定，回合正常收尾。"""
    import tools.clarify_gateway as _cg

    monkeypatch.setattr(_cg, "get_clarify_timeout", lambda: 0.2)
    _install, holder = stub_agent_factory
    agent = _install(clarify_call=("继续吗？", None, False))
    session = chat_api.ChatSession(chat_session_id="chat_tmo", agent=agent, session_db=None)
    chat_api._CHAT_SESSIONS[session.chat_session_id] = session
    loop = _SyncLoop()
    queue: pyqueue.Queue = pyqueue.Queue()
    try:
        t_worker, t_collect, events, stop = _run_turn_in_thread(session, queue, loop)
        ev = _wait_clarify(events)
        assert ev["timeout_at"]
        _wait_done(events)
        t_worker.join(timeout=5)
        stop.set()
        t_collect.join(timeout=2)
        assert agent.clarify_result == "[user did not respond within 1m]"
        assert session.pending_clarify is None
    finally:
        chat_api.clear_chat_sessions()


def test_clarify_multi_session_independent(stub_agent_factory):
    """多 session 并行 clarify 互不干扰：只答 A，B 仍挂起；再答 B 各自继续。"""
    _install, holder = stub_agent_factory
    agent_a = _install(clarify_call=("A 的问题", ["a1"], False))
    agent_b = _StubAgent(clarify_call=("B 的问题", ["b1"], False))
    # 简单做法：直接注册两个会话，第二个的 agent 手动指定。
    session_a = chat_api.ChatSession(chat_session_id="chat_a", agent=agent_a, session_db=None)
    session_b = chat_api.ChatSession(chat_session_id="chat_b", agent=agent_b, session_db=None)
    chat_api._CHAT_SESSIONS[session_a.chat_session_id] = session_a
    chat_api._CHAT_SESSIONS[session_b.chat_session_id] = session_b

    loop = _SyncLoop()
    queue: pyqueue.Queue = pyqueue.Queue()
    events: list = []
    stop_a = threading.Event()
    stop_b = threading.Event()

    t_ca = threading.Thread(target=_collect_into, args=(queue, events, stop_a), daemon=True)
    t_ca.start()

    t_a = threading.Thread(target=chat_api._run_chat_turn, args=(session_a, "hi", queue, loop), daemon=True)
    t_b = threading.Thread(target=chat_api._run_chat_turn, args=(session_b, "hi", queue, loop), daemon=True)
    t_a.start()
    t_b.start()

    try:
        _wait_event_count(events, "chat:clarify_pending", 2)
        # 只答 A。
        session_a.pending_clarify.response = "a1"
        session_a.pending_clarify.event.set()
        _wait_event_count(events, "chat:done", 1)
        t_a.join(timeout=5)
        assert agent_a.clarify_result == "a1"
        # B 仍挂起（未被 A 的应答影响）。
        assert session_b.pending_clarify is not None
        assert agent_b.clarify_result is None
        # 再答 B。
        session_b.pending_clarify.response = "b1"
        session_b.pending_clarify.event.set()
        _wait_done(events)
        t_b.join(timeout=5)
        assert agent_b.clarify_result == "b1"
    finally:
        stop_a.set()
        stop_b.set()
        t_ca.join(timeout=2)
        chat_api.clear_chat_sessions()


# ── HTTP 端点契约 ─────────────────────────────────────────────────────────


def test_clarify_endpoint_contract(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install()
    sid = _new_session(client)

    # 无挂起 → 409 no_pending_clarify
    resp = client.post(f"/api/chat/sessions/{sid}/clarify", json={"answer": "prod"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_pending_clarify"

    # 缺 answer → 400
    session = chat_api._CHAT_SESSIONS[sid]
    session.pending_clarify = chat_api._WebClarifyEntry(
        clarify_id="clfy_test",
        question="q",
        choices=["a", "b"],
        multi_select=False,
        timeout_at="2099-01-01T00:00:00Z",
    )
    resp = client.post(f"/api/chat/sessions/{sid}/clarify", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"

    # 正常应答 → 200 resolved + 条目被写 response 并唤醒
    resp = client.post(f"/api/chat/sessions/{sid}/clarify", json={"answer": "prod"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["clarify_id"] == "clfy_test"
    assert session.pending_clarify.response == "prod"
    assert session.pending_clarify.event.is_set()

    # 会话不存在 → 404
    resp = client.post("/api/chat/sessions/nope/clarify", json={"answer": "x"})
    assert resp.status_code == 404


def test_clarify_endpoint_timed_out_409(client, stub_agent_factory):
    _install, _ = stub_agent_factory
    _install()
    sid = _new_session(client)
    session = chat_api._CHAT_SESSIONS[sid]
    session.pending_clarify = chat_api._WebClarifyEntry(
        clarify_id="clfy_tmo",
        question="q",
        choices=None,
        multi_select=False,
        timeout_at="2020-01-01T00:00:00Z",  # 已过期
    )
    resp = client.post(f"/api/chat/sessions/{sid}/clarify", json={"answer": "x"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "clarify_timed_out"


def test_clarify_answer_not_logged(client, stub_agent_factory, caplog):
    """answer 不落日志（敏感答复如 sudo 密码不回显）。"""
    import logging

    _install, _ = stub_agent_factory
    _install()
    sid = _new_session(client)
    session = chat_api._CHAT_SESSIONS[sid]
    session.pending_clarify = chat_api._WebClarifyEntry(
        clarify_id="clfy_sec",
        question="sudo 密码是什么？",
        choices=None,
        multi_select=False,
        timeout_at="2099-01-01T00:00:00Z",
    )
    secret = "svp_secret_x9z42"
    with caplog.at_level(logging.DEBUG, logger="hermes_cli.chat_api"):
        resp = client.post(f"/api/chat/sessions/{sid}/clarify", json={"answer": secret})
        assert resp.status_code == 200
    assert secret not in resp.text
    assert not any(secret in r.message for r in caplog.records)
