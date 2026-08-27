"""批八十一：会话 context 使用率可见 + 生命周期健壮性（~/notes/vigil-batch81-prompt.md）。

分层：只打 chat_api / web_server 新增契约与引擎守卫，agent 用 stub（不触发真
LLM/工具）。覆盖：
  1. /api/sessions 与 /api/chat/sessions 的 context_usage（used/limit/pct/model；
     limit 三级：config 显式 > 内置表 > None；未计量 → null 不瞎猜）；既有字段
     零变化。
  2. 2a：ended 会话（外部终态）reopen 被拒 409 session_ended；turn 收尾中间态
     （ended+turn_complete）可继续。
  3. 2b：模型挂起/超时 → 友好文案"模型响应超时，已中止（可重试或新开会话）"。
  4. 2c：压缩后自检（空/非列表/缺 system+user）→ 抛错 + 会话落 needs_recovery。
  5. 2d：context 用量 >=80% → SSE 推 chat:context_warning。
  6. token_count 落库读回（session_used_tokens 口径）。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _CompressorStub:
    def __init__(self, last_prompt_tokens: int):
        self.last_prompt_tokens = last_prompt_tokens


class _StubAgent:
    """可脚本化 agent stub：turn 行为可注入（raise_exc 模拟挂起/超时）。"""

    _session_db = None

    def __init__(self, raise_exc: Exception = None):
        self.calls = []
        self.raise_exc = raise_exc
        self.model = "qwen3.5-397b"
        self.context_compressor = None
        self.session_id = None
        self.clarify_callback = None
        self.reasoning_callback = None

    def run_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        sc = kwargs.get("stream_callback")
        if sc:
            sc("Hello World")
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

    def _install(agent: _StubAgent = None) -> _StubAgent:
        agent = agent or _StubAgent()
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


def _seed_db() -> "SessionDB":
    from hermes_state import SessionDB
    return SessionDB()


# ── 任务 1：/api/sessions context_usage（limit 三级 + 未计量） ───────────────


def test_api_sessions_context_usage_builtin_limit(client, env_home):
    """内置默认表命中：qwen3.5-397b → 131072；used=SUM(token_count)。"""
    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    db.append_message("s1", "system", content="sys", token_count=100)
    db.append_message("s1", "user", content="hello", token_count=50)
    db.append_message("s1", "assistant", content="hi", token_count=30)
    db.close()

    resp = client.get("/api/sessions")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["sessions"]
    row = next(r for r in rows if r.get("session_id") == "s1")
    cu = row["context_usage"]
    assert cu["used_tokens"] == 180
    assert cu["limit_tokens"] == 131072
    assert cu["pct"] == round(180 * 100 / 131072)
    assert cu["model"] == "qwen3.5-397b"


def test_api_sessions_context_usage_config_explicit_limit(client, env_home):
    """config 显式 model.context_length 优先于内置表。"""
    from hermes_cli.config import load_config, save_config
    cfg = load_config()
    cfg["model"] = {"default": "qwen3.5-397b", "context_length": 100000}
    save_config(cfg)

    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    db.append_message("s1", "user", content="hello", token_count=40)
    db.close()

    resp = client.get("/api/sessions")
    row = next(r for r in resp.json()["sessions"] if r.get("session_id") == "s1")
    assert row["context_usage"]["limit_tokens"] == 100000
    assert row["context_usage"]["pct"] == 0  # 40 / 100000 → 0%


def test_api_sessions_context_usage_unknown_model(client, env_home):
    """内置表也没有 → limit_tokens None → pct None（前端显示"—"）。"""
    db = _seed_db()
    db.create_session("s1", source="web", model="bogus-model-xyz")
    db.append_message("s1", "user", content="hello", token_count=40)
    db.close()

    resp = client.get("/api/sessions")
    row = next(r for r in resp.json()["sessions"] if r.get("session_id") == "s1")
    cu = row["context_usage"]
    assert cu["limit_tokens"] is None
    assert cu["pct"] is None
    assert cu["used_tokens"] == 40


def test_api_sessions_context_usage_unmeasured(client, env_home):
    """有消息但 token_count 未计量（本批之前的历史行）→ used_tokens None。"""
    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    db.append_message("s1", "user", content="hello")  # 无 token_count
    db.close()

    resp = client.get("/api/sessions")
    row = next(r for r in resp.json()["sessions"] if r.get("session_id") == "s1")
    assert row["context_usage"]["used_tokens"] is None
    assert row["context_usage"]["pct"] is None


def test_api_sessions_existing_fields_zero_change(client, env_home):
    """既有字段零破坏：session_id/last_activity_at/running 照常返回。"""
    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    db.append_message("s1", "user", content="hello", token_count=10)
    db.close()

    resp = client.get("/api/sessions")
    row = next(r for r in resp.json()["sessions"] if r.get("session_id") == "s1")
    assert row["session_id"] == "s1"
    assert "last_activity_at" in row
    assert "running" in row
    assert set(row.keys()) >= {"session_id", "last_activity_at", "running", "context_usage"}


# ── 任务 1：/api/chat/sessions view context_usage（活会话兜底） ──────────────


def test_chat_sessions_view_context_usage_live_fallback(client, env_home, stub_agent_factory):
    """活会话 DB 未计量（有消息无 token_count）→ 回退 compressor.last_prompt_tokens。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    agent.context_compressor = _CompressorStub(last_prompt_tokens=35000)
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.append_message(sid, "user", content="hello")  # 无 token_count → 未计量

    resp = client.get("/api/chat/sessions")
    rows = resp.json()["sessions"]
    row = next(r for r in rows if r["id"] == sid)
    cu = row["context_usage"]
    assert cu["used_tokens"] == 35000
    assert cu["limit_tokens"] == 131072
    assert cu["pct"] == round(35000 * 100 / 131072)


def test_chat_sessions_view_context_usage_measured(client, env_home, stub_agent_factory):
    """DB 已计量 → 用 DB 累计（compressor 兜底不覆盖）。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    agent.context_compressor = _CompressorStub(last_prompt_tokens=35000)
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.append_message(sid, "user", content="hello", token_count=500)
    db.append_message(sid, "assistant", content="hi", token_count=400)

    resp = client.get("/api/chat/sessions")
    row = next(r for r in resp.json()["sessions"] if r["id"] == sid)
    assert row["context_usage"]["used_tokens"] == 900


# ── 2a：ended 会话 reopen 收口 ──────────────────────────────────────────────


def test_2a_ended_session_external_reject(client, env_home, stub_agent_factory):
    """外部终态（end_reason=user_request）→ 发消息被拒 409 session_ended。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.end_session(sid, "user_request")

    resp = client.post(f"/api/chat/sessions/{sid}/messages", json={"message": "hi"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "session_ended"
    assert "请新开会话" in resp.json()["error"]["message"]


def test_2a_ended_session_turn_complete_allowed(client, env_home, stub_agent_factory):
    """turn 收尾中间态（ended+turn_complete）→ 可继续（下轮复位 running）。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.set_session_status(sid, "ended", end_reason="turn_complete", ended_at=time.time())

    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    assert any(ev == "chat:done" for ev, _ in events), events


def test_2a_needs_recovery_reject(client, env_home, stub_agent_factory):
    """needs_recovery（压缩自检异常）→ 发消息被拒。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.set_session_status(sid, "needs_recovery")

    resp = client.post(f"/api/chat/sessions/{sid}/messages", json={"message": "hi"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "session_ended"
    assert "需恢复" in resp.json()["error"]["message"]


# ── 2b：模型挂起/超时友好文案 ────────────────────────────────────────────────


def test_2b_timeout_error_friendly_message(client, env_home, stub_agent_factory):
    """TimeoutError（stale 检测器）→ chat:error 文案"模型响应超时，已中止"。"""
    agent = _StubAgent(raise_exc=TimeoutError("Non-streaming API call timed out after 90s with no response"))
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    errs = [d for ev, d in events if ev == "chat:error"]
    assert errs
    assert "模型响应超时，已中止（可重试或新开会话）" in errs[0]["message"]


def test_2b_stale_streak_error_friendly_message(client, env_home, stub_agent_factory):
    """连续 stale giveup（RuntimeError）→ 同样映射友好文案。"""
    agent = _StubAgent(raise_exc=RuntimeError(
        "Provider has been unresponsive (no response received) for 5 consecutive "
        "stale attempts — aborting this call to avoid an indefinite stall."
    ))
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    errs = [d for ev, d in events if ev == "chat:error"]
    assert errs
    assert "模型响应超时，已中止（可重试或新开会话）" in errs[0]["message"]


def test_2b_other_error_kept_verbatim(client, env_home, stub_agent_factory):
    """非超时错误保持原始类型+信息（便于排查）。"""
    agent = _StubAgent(raise_exc=ValueError("boom"))
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "hi"})
    errs = [d for ev, d in events if ev == "chat:error"]
    assert errs
    assert errs[0]["message"] == "ValueError: boom"


# ── 2d：context >=80% 主动提示 ───────────────────────────────────────────────


def test_2d_context_warning_fired(client, env_home, stub_agent_factory):
    """DB 未计量（旧会话）+ 活 compressor 120000 / 131072 ≈ 92% → chat:context_warning。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    agent.context_compressor = _CompressorStub(last_prompt_tokens=120000)
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.append_message(sid, "user", content="hello")  # 未计量

    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "继续"})
    warns = [d for ev, d in events if ev == "chat:context_warning"]
    assert warns, events
    assert warns[0]["pct"] >= 80
    assert "建议新开会话" in warns[0]["message"]


def test_2d_context_warning_not_fired_low(client, env_home, stub_agent_factory):
    """用量低 → 不推警告。"""
    db = _seed_db()
    agent = _StubAgent()
    agent._session_db = db
    agent.context_compressor = _CompressorStub(last_prompt_tokens=10000)
    stub_agent_factory[0](agent)
    sid = _new_session(client)
    db.create_session(sid, source="web", model="qwen3.5-397b")
    db.append_message(sid, "user", content="hello")  # 未计量

    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", {"message": "继续"})
    assert not any(ev == "chat:context_warning" for ev, _ in events)


# ── 2c：压缩后自检 → needs_recovery ─────────────────────────────────────────


def _verify_agent() -> "AIAgent-like":
    from agent.conversation_loop import _verify_post_compression_messages  # noqa: F401
    from types import SimpleNamespace
    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    agent = SimpleNamespace(_session_db=db, session_id="s1")
    return agent


def test_2c_verify_ok_passes():
    from agent.conversation_loop import _verify_post_compression_messages
    agent = _verify_agent()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    _verify_post_compression_messages(agent, messages, site="pre_api")  # 不抛
    assert agent._session_db.get_session("s1")["status"] == "running"


def test_2c_verify_empty_marks_needs_recovery():
    from agent.conversation_loop import _verify_post_compression_messages
    agent = _verify_agent()
    with pytest.raises(RuntimeError, match="needs_recovery|已标记需恢复"):
        _verify_post_compression_messages(agent, [], site="pre_api")
    assert agent._session_db.get_session("s1")["status"] == "needs_recovery"


def test_2c_verify_missing_user_marks_needs_recovery():
    from agent.conversation_loop import _verify_post_compression_messages
    agent = _verify_agent()
    with pytest.raises(RuntimeError):
        _verify_post_compression_messages(agent, [{"role": "system", "content": "sys"}], site="pre_api")
    assert agent._session_db.get_session("s1")["status"] == "needs_recovery"


def test_2c_verify_non_list_marks_needs_recovery():
    from agent.conversation_loop import _verify_post_compression_messages
    agent = _verify_agent()
    with pytest.raises(RuntimeError):
        _verify_post_compression_messages(agent, None, site="pre_api")
    assert agent._session_db.get_session("s1")["status"] == "needs_recovery"


def test_2c_finalize_preserves_needs_recovery(env_home):
    """turn 收尾不得把 needs_recovery 覆盖降级回 ended。"""
    from hermes_cli import chat_api
    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    db.set_session_status("s1", "needs_recovery")
    session = chat_api.ChatSession(chat_session_id="s1", agent=_StubAgent(), session_db=db)
    chat_api._finalize_turn_session(session, interrupted=False, failed=True)
    assert db.get_session("s1")["status"] == "needs_recovery"
    db.close()


# ── token_count 落库读回（session_used_tokens 口径） ────────────────────────


def test_session_used_tokens_measured_and_unmeasured(env_home):
    from hermes_cli.session_context_usage import session_used_tokens
    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    # 空会话 → 0
    assert session_used_tokens(db, "s1") == 0
    # 有消息但未计量 → None
    db.append_message("s1", "user", content="hello")
    assert session_used_tokens(db, "s1") is None
    # 计量后 → 累计
    db.append_message("s1", "assistant", content="hi", token_count=123)
    assert session_used_tokens(db, "s1") == 123
    db.close()


def test_flush_writes_token_count(env_home):
    """run_agent 持久化路径逐条写 token_count（本批补齐的列）。"""
    from run_agent import AIAgent
    from hermes_state import SessionDB

    db = _seed_db()
    db.create_session("s1", source="web", model="qwen3.5-397b")
    agent = object.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = "s1"
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent._db_flush_scan_prefix = None
    agent._persist_disabled = False

    agent._flush_messages_to_session_db([
        {"role": "user", "content": "hello world 你好"},
        {"role": "assistant", "content": "hi there"},
    ])
    rows = db.get_messages("s1")
    assert len(rows) == 2
    for row in rows:
        assert (row.get("token_count") or 0) > 0, row
    db.close()
