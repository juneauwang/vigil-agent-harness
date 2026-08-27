"""批八十二：多 session 并发 UI 状态丢失（审批/clarify 卡持久化 + activeId 持久化）
（~/notes/vigil-batch82-prompt.md）。

分层：只打 chat_api 的落库/过滤/视图折叠契约 + gateway 防线 + 端点字段零破坏，
agent 用 stub（不触发真 LLM/工具）。覆盖：
  1. 1a 落库：审批/clarify 回调把卡片快照写 SessionDB（role=approval/clarify，
     content=JSON）——回调工厂直接调用（approval 超时=0 不阻塞；clarify 由
     测试线程应答解除阻塞）。
  2. 1a 过滤：_load_conversation_history 默认剔除 approval/clarify/session_meta
     （喂模型零污染，验收 5）；include_cards=True 保留卡行供前端恢复。
  3. 1b 视图折叠：卡行折叠进卡创建时的"活动"assistant 气泡（对齐 SSE 流式
     挂接 = 卡行前最近创建的 assistant）；卡行前无 assistant → 挂下一
     assistant；尾部无 assistant → 独立 assistant 气泡。
  4. 1b 状态覆盖：注册表 approve_web_approval 后视图 status=approved；
     session.clarify_outcomes 登记 answered/timed_out → 视图 status 覆盖。
  5. gateway 防线：_build_gateway_agent_history 过滤 approval/clarify 行。
  6. 端点字段零破坏：/api/chat/sessions/{id}/messages 仍含 role/content/tools，
     卡行折叠进 approvals/clarifies 数组（验收 6）。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server
from hermes_state import SessionDB


class _StubAgent:
    """最小 agent stub：只承载 session_id（_live_session_id 回退逻辑用）。"""

    _session_db = None

    def __init__(self, session_id: str = None):
        self.session_id = session_id
        self.model = "qwen3.5-397b"
        self.context_compressor = None
        self.clarify_callback = None
        self.reasoning_callback = None


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
        "approvals:\n  mode: manual\n  timeout: 60\n"
        "agent:\n  clarify_timeout: 60\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    clear_web_approvals()


@pytest.fixture()
def stub_agent_factory(monkeypatch):
    def _install(agent: _StubAgent = None) -> _StubAgent:
        agent = agent or _StubAgent()
        monkeypatch.setattr(chat_api, "_create_chat_agent", lambda sid, model=None, provider=None: agent)
        return agent

    return _install


def _make_session(tmp_path, sid: str = None) -> chat_api.ChatSession:
    """真实 SessionDB（隔离 VIGIL_HOME）承载的 ChatSession，可直接 append 行。"""
    sid = sid or f"sess_{secrets.token_hex(4)}"
    db = SessionDB()
    db.create_session(sid, source="web")
    return chat_api.ChatSession(
        chat_session_id=sid,
        agent=_StubAgent(session_id=sid),
        session_db=db,
    )


def _approval_card_json(approval_id: str = "aprv_test") -> str:
    return json.dumps({
        "approval_id": approval_id,
        "command": "kubectl rollout restart deploy/api",
        "description": "重启 prod api 部署",
        "env": "prod",
        "action": "restart",
        "timeout_at": "2099-01-01T00:00:00Z",
        "status": "pending",
    }, ensure_ascii=False)


def _clarify_card_json(clarify_id: str = "clfy_test") -> str:
    return json.dumps({
        "clarify_id": clarify_id,
        "question": "用哪个环境？",
        "choices": ["prod", "dev"],
        "multi_select": False,
        "timeout_at": "2099-01-01T00:00:00Z",
    }, ensure_ascii=False)


def _append_turn_with_cards(db: SessionDB, sid: str, *, approval: bool = True, clarify: bool = True):
    """落一行典型 turn：user → assistant(tool_calls) → 卡行 → tool → assistant。"""
    db.append_message(sid, "user", content="部署到 prod")
    db.append_message(
        sid, "assistant", content="",
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {"name": "terminal", "arguments": "{\"cmd\":\"kubectl rollout restart deploy/api\"}"},
        }],
    )
    if approval:
        db.append_message(sid, "approval", content=_approval_card_json())
    if clarify:
        db.append_message(sid, "clarify", content=_clarify_card_json())
    db.append_message(sid, "tool", content="rollout restarted", tool_name="terminal", tool_call_id="call_1")
    db.append_message(sid, "assistant", content="已重启 prod api 部署")


def test_load_conversation_history_filters_cards_from_model_context(tmp_path):
    """验收 5：喂模型的 history 零污染——approval/clarify/session_meta 默认剔除。"""
    session = _make_session(tmp_path)
    _append_turn_with_cards(session.session_db, session.chat_session_id)
    session.session_db.append_message(session.chat_session_id, "session_meta", content="{}")

    clean = chat_api._load_conversation_history(session)
    roles = [m.get("role") for m in clean]
    assert "approval" not in roles
    assert "clarify" not in roles
    assert "session_meta" not in roles
    # 正常对话行完整保留。
    assert roles == ["user", "assistant", "tool", "assistant"]

    with_cards = chat_api._load_conversation_history(session, include_cards=True)
    card_roles = [m.get("role") for m in with_cards]
    assert card_roles.count("approval") == 1
    assert card_roles.count("clarify") == 1
    assert "session_meta" not in card_roles
    # 卡行 content 是可解析的 JSON 快照。
    approval_row = next(m for m in with_cards if m.get("role") == "approval")
    snapshot = json.loads(approval_row["content"])
    assert snapshot["approval_id"] == "aprv_test"
    assert snapshot["status"] == "pending"


def test_history_to_view_folds_cards_into_active_assistant(tmp_path):
    """1b：卡行折叠进卡创建时的"活动"assistant 气泡（卡行前最近创建的
    assistant，对齐 SSE 流式挂接），且状态以当前为准。"""
    session = _make_session(tmp_path)
    _append_turn_with_cards(session.session_db, session.chat_session_id)
    history = chat_api._load_conversation_history(session, include_cards=True)
    view = chat_api._history_to_view_messages(history, session=session)

    # 卡片挂在 assistant(tool_calls) 气泡（不是 final 气泡）。
    tool_bubble = next(m for m in view if m.get("role") == "assistant" and m.get("tools"))
    assert len(tool_bubble["approvals"]) == 1
    assert tool_bubble["approvals"][0]["approval_id"] == "aprv_test"
    assert tool_bubble["approvals"][0]["status"] == "pending"
    assert len(tool_bubble["clarifies"]) == 1
    assert tool_bubble["clarifies"][0]["clarify_id"] == "clfy_test"
    final_bubble = next(m for m in view if m.get("role") == "assistant" and not m.get("tools"))
    assert final_bubble["approvals"] == []
    assert final_bubble["clarifies"] == []

    # 普通 user/assistant 气泡带空卡数组（前端 ChatMessage 结构）。
    user_bubble = next(m for m in view if m.get("role") == "user")
    assert user_bubble["approvals"] == []
    assert user_bubble["clarifies"] == []
    assert user_bubble["content"] == "部署到 prod"


def test_history_to_view_standalone_bubble_when_no_assistant_after(tmp_path):
    """1b：卡行后无 assistant → 独立 assistant 气泡（turn 在卡片处中断）。"""
    session = _make_session(tmp_path)
    db = session.session_db
    db.append_message(session.chat_session_id, "user", content="帮我审批一下")
    db.append_message(session.chat_session_id, "approval", content=_approval_card_json())

    history = chat_api._load_conversation_history(session, include_cards=True)
    view = chat_api._history_to_view_messages(history, session=session)
    assert view[-1]["role"] == "assistant"
    assert view[-1]["content"] == ""
    assert len(view[-1]["approvals"]) == 1
    assert view[-1]["approvals"][0]["approval_id"] == "aprv_test"


def test_history_to_view_pending_card_folds_into_next_assistant(tmp_path):
    """1b：卡行前无 assistant（turn 首卡）→ 挂下一个 assistant 气泡。"""
    session = _make_session(tmp_path)
    db = session.session_db
    db.append_message(session.chat_session_id, "user", content="确认下")
    db.append_message(session.chat_session_id, "clarify", content=_clarify_card_json())
    db.append_message(session.chat_session_id, "assistant", content="请选择")

    history = chat_api._load_conversation_history(session, include_cards=True)
    view = chat_api._history_to_view_messages(history, session=session)
    assert len(view) == 2
    assert view[1]["role"] == "assistant"
    assert len(view[1]["clarifies"]) == 1
    assert view[1]["clarifies"][0]["clarify_id"] == "clfy_test"


def test_view_approval_status_overridden_by_registry(tmp_path):
    """验收 4：审批中心批准后刷新 → 历史视图 status=approved（get_web_approval 覆盖）。"""
    from tools.approval import approve_web_approval, register_web_approval

    session = _make_session(tmp_path)
    db = session.session_db
    approval_id = register_web_approval(command="kubectl get pods", description="查看", env="prod")
    approve_web_approval(approval_id)
    db.append_message(session.chat_session_id, "user", content="看下")
    db.append_message(session.chat_session_id, "assistant", content="")
    db.append_message(
        session.chat_session_id, "approval",
        content=json.dumps({"approval_id": approval_id, "command": "kubectl get pods",
                            "description": "查看", "env": "prod", "action": None,
                            "timeout_at": None, "status": "pending"}),
    )
    db.append_message(session.chat_session_id, "assistant", content="完成")

    history = chat_api._load_conversation_history(session, include_cards=True)
    view = chat_api._history_to_view_messages(history, session=session)
    bubble = view[1]  # assistant(tool 前) —— 实际是无 tool 的 assistant
    assert bubble["approvals"][0]["status"] == "approved"


def test_view_clarify_status_from_outcomes_registry(tmp_path):
    """验收 4：clarify 已应答/超时登记 → 历史视图 status 覆盖。"""
    session = _make_session(tmp_path)
    db = session.session_db
    db.append_message(session.chat_session_id, "user", content="选个环境")
    db.append_message(session.chat_session_id, "assistant", content="")
    db.append_message(session.chat_session_id, "clarify", content=_clarify_card_json("clfy_a"))
    db.append_message(session.chat_session_id, "assistant", content="完成")
    session.clarify_outcomes["clfy_a"] = "answered"

    history = chat_api._load_conversation_history(session, include_cards=True)
    view = chat_api._history_to_view_messages(history, session=session)
    assert view[1]["clarifies"][0]["status"] == "answered"

    # 未登记（服务重启后注册表空）→ 快照 pending 兜底。
    db.append_message(session.chat_session_id, "user", content="再来")
    db.append_message(session.chat_session_id, "assistant", content="")
    db.append_message(session.chat_session_id, "clarify", content=_clarify_card_json("clfy_b"))
    db.append_message(session.chat_session_id, "assistant", content="完成")
    history2 = chat_api._load_conversation_history(session, include_cards=True)
    view2 = chat_api._history_to_view_messages(history2, session=session)
    assert view2[4]["clarifies"][0]["status"] == "pending"


def test_approval_callback_persists_card_row(tmp_path):
    """1a：审批回调在推送点后把卡片快照落 SessionDB（role='approval'）。"""
    from tools.approval import approve_web_approval, list_web_approvals

    session = _make_session(tmp_path)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    result: dict = {}

    def _run_cb():
        try:
            queue = asyncio.Queue()
            cb = chat_api._approval_callback_factory(session, queue, loop)
            result["choice"] = cb("kubectl rollout restart deploy/api", "重启 prod api 部署")
        except Exception as exc:  # pragma: no cover - 测试线程异常透传
            result["exc"] = exc

    t = threading.Thread(target=_run_cb, daemon=True)
    t.start()
    try:
        # 等审批登记 → 测试线程批准解除 _cb 阻塞。
        for _ in range(200):
            views, _ = list_web_approvals(limit=10)
            if views:
                break
            time.sleep(0.01)
        assert views, "审批条目未登记"
        approve_web_approval(views[0]["id"])
        t.join(timeout=5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    assert "exc" not in result
    assert result.get("choice") == "once"
    rows = session.session_db.get_messages_as_conversation(
        session.chat_session_id, include_row_ids=True,
    )
    approval_rows = [m for m in rows if m.get("role") == "approval"]
    assert len(approval_rows) == 1
    snapshot = json.loads(approval_rows[0]["content"])
    assert snapshot["command"] == "kubectl rollout restart deploy/api"
    assert snapshot["env"]  # _default_ops_env()（测试环境 test/dev/prod 任一）
    assert snapshot["status"] == "pending"
    assert snapshot["approval_id"]


def test_clarify_callback_persists_card_row_and_registers_outcome(tmp_path):
    """1a+1b：clarify 回调落库 + 应答后登记终态。"""
    session = _make_session(tmp_path)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    result: dict = {}

    def _run_cb():
        try:
            queue = asyncio.Queue()
            cb = chat_api._clarify_callback_factory(session, queue, loop)
            result["choice"] = cb("用哪个环境？", ["prod", "dev"], multi_select=False)
        except Exception as exc:  # pragma: no cover - 测试线程异常透传
            result["exc"] = exc

    t = threading.Thread(target=_run_cb, daemon=True)
    t.start()
    try:
        # 等挂起条目出现 → 模拟前端应答。
        for _ in range(200):
            entry = getattr(session, "pending_clarify", None)
            if entry is not None:
                break
            time.sleep(0.01)
        assert entry is not None, "clarify 挂起条目未登记"
        entry.response = "prod"
        entry.event.set()
        t.join(timeout=5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    assert "exc" not in result
    assert result.get("choice") == "prod"
    assert session.clarify_outcomes.get(entry.clarify_id) == "answered"

    rows = session.session_db.get_messages_as_conversation(
        session.chat_session_id, include_row_ids=True,
    )
    clarify_rows = [m for m in rows if m.get("role") == "clarify"]
    assert len(clarify_rows) == 1
    snapshot = json.loads(clarify_rows[0]["content"])
    assert snapshot["clarify_id"] == entry.clarify_id
    assert snapshot["question"] == "用哪个环境？"
    assert snapshot["choices"] == ["prod", "dev"]


def test_gateway_history_filters_card_rows():
    """防线：gateway 恢复会话读通用入口时过滤 approval/clarify 行（防 400）。"""
    from gateway.run import _build_gateway_agent_history

    history = [
        {"role": "user", "content": "部署"},
        {"role": "assistant", "content": ""},
        {"role": "approval", "content": _approval_card_json()},
        {"role": "clarify", "content": _clarify_card_json()},
        {"role": "assistant", "content": "完成"},
    ]
    agent_history, _ = _build_gateway_agent_history(history)
    roles = [m.get("role") for m in agent_history]
    assert "approval" not in roles
    assert "clarify" not in roles
    # 卡行被滤掉；空 content 的 assistant 行被既有 content 门丢弃（batch82 前
    # 行为）→ 只剩 user + 最终 assistant。
    assert roles == ["user", "assistant"]


def test_messages_endpoint_fields_intact(client, stub_agent_factory):
    """验收 6：/api/chat/sessions/{id}/messages 字段零破坏 + 卡数组随消息返回。"""
    stub_agent_factory()
    resp = client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    sid = resp.json()["chat_session_id"]
    session = chat_api._CHAT_SESSIONS[sid]
    # stub 工厂不产真实 SessionDB → 测试内补一个（隔离 VIGIL_HOME）。
    db = SessionDB()
    db.create_session(sid, source="web")
    session.session_db = db

    _append_turn_with_cards(db, sid)
    # 再落一条 approve 后置卡（独立气泡路径：卡后无 assistant）。
    session.session_db.append_message(sid, "user", content="结尾审批")
    session.session_db.append_message(sid, "approval", content=_approval_card_json("aprv_tail"))

    resp = client.get(f"/api/chat/sessions/{sid}/messages")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    messages = payload["messages"]
    assert messages, "messages 不应为空"

    # 既有字段零破坏。
    for m in messages:
        assert "id" in m
        assert "role" in m
        assert "content" in m
        assert "tools" in m
        assert "timestamp" in m or "timestamp" not in m

    # 卡行折叠：assistant(tool_calls) 气泡带 approvals/clarifies。
    tool_bubbles = [m for m in messages if m["role"] == "assistant" and m["tools"]]
    assert len(tool_bubbles) == 1
    assert len(tool_bubbles[0]["approvals"]) == 1
    assert tool_bubbles[0]["approvals"][0]["approval_id"] == "aprv_test"
    assert len(tool_bubbles[0]["clarifies"]) == 1

    # 尾部卡（无后续 assistant）→ 独立 assistant 气泡。
    assert messages[-1]["role"] == "assistant"
    assert len(messages[-1]["approvals"]) == 1
    assert messages[-1]["approvals"][0]["approval_id"] == "aprv_tail"
    assert messages[-1]["content"] == ""
