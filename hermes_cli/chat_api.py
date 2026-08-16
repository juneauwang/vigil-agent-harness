"""Vigil Console —— 对话 Session API（UI 壳核心价值页 /api/chat/*）。

会话是进程内注册表（照 approval 注册表模式）：``dict[chat_session_id → ChatSession]``，
重启即丢（已知边界）。每个会话持有一个常驻 AIAgent 实例（跨 turn 缓存，压缩状态/
记忆保留，gateway 同款驱动模式：``agent.run_conversation(msg, conversation_history=…,
task_id=…)``）；同一会话串行（busy → 409，前端禁用输入直到 done）。

SSE 事件命名照批二十八 exec API 风格：
  - ``chat:delta``           文本增量（打字机渲染）
  - ``chat:tool``            工具调用开始（T1 run_conversation tool_callback 转发）
  - ``chat:tool_result``     工具结果摘要（redact 后）
  - ``chat:approval_pending`` 审批挂起（带 approval_id；审批走现有 /api/approvals）
  - ``chat:done``            完成（完整回复 + token 统计）
  - ``chat:error``           错误

安全：新端点不进 PUBLIC_API_PATHS，走既有 dashboard 鉴权；工具输入/输出摘要与
审批展示文本全部过双层 redact（redact_sensitive_text + topo_export._redact_value）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, StreamingResponse

_log = logging.getLogger(__name__)

# 会话注册表（进程内，重启丢——OPS-DELTA 已知边界）。
_CHAT_SESSIONS: Dict[str, "ChatSession"] = {}
_CHAT_LOCK = threading.Lock()
_CHAT_SESSIONS_MAX = 20  # 软上限：超限淘汰最久未活动的空闲会话

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# 摘要/展示截断
_PREVIEW_MAX = 120


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact_text(value: Any) -> str:
    """双层脱敏（与 web_server._redact_exec_text 同套）：值级 + URL userinfo /
    .pem / home 前缀。失败原样返回。"""
    try:
        text = str(value)
    except Exception:
        return ""
    try:
        from agent.redact import redact_sensitive_text
        out = redact_sensitive_text(text, credential_values=True, force=True)
        from hermes_cli.subcommands.topo_export import _redact_value
        return _redact_value(out)
    except Exception:
        return text


def _preview(value: Any, max_len: int = _PREVIEW_MAX) -> str:
    """redact + 单行截断的展示预览。"""
    out = _redact_text(value).replace("\n", " ").strip()
    if len(out) > max_len:
        out = out[:max_len] + "…"
    return out


def _default_ops_env() -> str:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return str(cfg.get("ops", {}).get("permissions", {}).get("env") or "").strip().lower()
    except Exception:
        return ""


def _probe_ops_grade(command: str) -> Optional[str]:
    """审批条目 grade 探测（照 exec API：ops 权限矩阵 approve 时才带）。"""
    try:
        from tools.ops_permissions import check_ops_command_permission as _check_ops
        _ops = _check_ops(command)
        if _ops and _ops.get("action") == "approve":
            return _ops.get("grade")
    except Exception:
        _log.debug("chat approval grade probe failed", exc_info=True)
    return None


@dataclass
class ChatSession:
    chat_session_id: str
    agent: Any
    session_db: Any
    created_at: str = field(default_factory=_now_iso_utc)
    title: str = ""
    last_activity_at: float = field(default_factory=time.time)
    busy: bool = False
    _turn_done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._turn_done.set()

    def view(self) -> dict:
        return {
            "id": self.chat_session_id,
            "title": self.title,
            "created_at": self.created_at,
            "busy": self.busy,
            "last_message_preview": self.title,
        }


def _create_chat_agent(chat_session_id: str):
    """构造会话 agent（mirror oneshot 的非交互路径；平台标记 web）。

    模型/运行时照 config.yaml model.* + resolve_runtime_provider；工具集取
    platform_toolsets.cli（用户经 ``vigil tools`` 的既有配置）；MCP 在构造前
    幂等发现；会话历史落 SQLite SessionDB（session_id = chat_session_id）。
    """
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_state import SessionDB
    from run_agent import AIAgent

    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        cfg_model = model_cfg
    else:
        cfg_model = model_cfg.get("default") or model_cfg.get("model") or ""
    effective_model = str(cfg_model or "").strip()
    runtime = resolve_runtime_provider(target_model=effective_model or None)

    toolsets_list = sorted(_get_platform_tools(cfg, "cli"))
    ensure_mcp_discovery_before_agent_build(
        logger=_log,
        single_query=False,
    )
    session_db = SessionDB()
    try:
        session_db.create_session(chat_session_id, source="web")
    except Exception:
        _log.debug("chat session row create skipped", exc_info=True)
    _fb = get_fallback_chain(cfg)

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        requested_provider=runtime.get("requested_provider"),
        api_mode=runtime.get("api_mode"),
        acp_command=runtime.get("command"),
        acp_args=list(runtime.get("args") or []),
        credential_pool=runtime.get("credential_pool"),
        model=effective_model,
        max_tokens=runtime.get("max_tokens") or None,
        max_iterations=60,
        enabled_toolsets=toolsets_list,
        quiet_mode=True,
        platform="web",
        session_id=chat_session_id,
        session_db=session_db,
        fallback_model=_fb or None,
    )
    # suppress_status_output 是构造后属性（oneshot 同款），不是 init kwarg。
    agent.suppress_status_output = True
    return agent


def secrets_hex() -> str:
    import secrets
    return secrets.token_hex(4)


def _load_conversation_history(
    session: "ChatSession",
    *,
    repair_alternation: bool = True,
    include_row_ids: bool = False,
) -> list:
    """跨 turn 上下文：从 SessionDB 重放已持久化消息（gateway 同款）。

    历史展示端点传 ``repair_alternation=False``（verbatim 转录，不合并/丢弃
    消息）+ ``include_row_ids=True``（稳定 id 作前端 React key）。
    """
    if session.session_db is None:
        return []
    try:
        history = session.session_db.get_messages_as_conversation(
            session.chat_session_id,
            repair_alternation=repair_alternation,
            include_row_ids=include_row_ids,
        )
        return [m for m in history if m.get("role") != "session_meta"]
    except Exception:
        _log.debug("chat history reload failed", exc_info=True)
        return []


def _history_to_view_messages(history: list) -> list:
    """把 SessionDB 的 OpenAI 消息 dict 结构化为前端 ChatMessage 字段。

    返回消息列表（role/content/tools/timestamp，id 用行 id 保证稳定），内容
    全部过 redact（_redact_text 值级 + _preview 单行截断的展示预览）。tool
    调用（assistant.tool_calls）与紧随的 tool 结果行折叠进同一条 assistant
    气泡的 ``tools`` 数组（对齐 SSE 流式渲染的 ChatToolEvent）。
    """
    view: List[Dict[str, Any]] = []
    pending_tools: List[Dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        msg_id = msg.get("_row_id")
        if role == "user":
            view.append({
                "id": msg_id if msg_id is not None else len(view) + 1,
                "role": "user",
                "content": _redact_text(msg.get("content") or ""),
                "tools": [],
                "timestamp": msg.get("timestamp"),
            })
        elif role == "assistant":
            tools: List[Dict[str, Any]] = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tools.append({
                    "name": str(fn.get("name") or ""),
                    "input_summary": _preview(fn.get("arguments") or ""),
                    "output_summary": None,
                    "ok": None,
                })
            view.append({
                "id": msg_id if msg_id is not None else len(view) + 1,
                "role": "assistant",
                "content": _redact_text(msg.get("content") or ""),
                "tools": tools,
                "timestamp": msg.get("timestamp"),
            })
            pending_tools = tools
        elif role == "tool":
            content = _preview(msg.get("content") or "")
            name = str(msg.get("tool_name") or "")
            matched = False
            for t in reversed(pending_tools):
                if t.get("output_summary") is None and (not name or t.get("name") == name):
                    t["output_summary"] = content
                    t["ok"] = True
                    matched = True
                    break
            if not matched and name:
                view.append({
                    "id": msg_id if msg_id is not None else len(view) + 1,
                    "role": "assistant",
                    "content": "",
                    "tools": [{
                        "name": name,
                        "input_summary": "",
                        "output_summary": content,
                        "ok": True,
                    }],
                    "timestamp": msg.get("timestamp"),
                })
                pending_tools = []
        # session_meta 已在上游过滤，这里兜底跳过。
    for t in pending_tools:
        if t.get("output_summary") is None:
            t["output_summary"] = ""
    return view


def _approval_callback_factory(session: "ChatSession", queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """终端工具审批回调（线程内调用）：登记 web 审批 → 推送 chat:approval_pending →
    阻塞等裁决（wait 语义）→ 返回选择串。"""
    from tools.approval import register_web_approval, wait_web_approval

    def _push(event: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
        except Exception:
            _log.debug("chat approval event push failed", exc_info=True)

    def _cb(command: str, description: str, *,
            allow_permanent: bool = True, allow_session: bool = True,
            smart_denied: bool = False) -> str:
        env = _default_ops_env()
        grade = _probe_ops_grade(command)
        redacted_command = _redact_text(command)
        approval_id = register_web_approval(
            command=redacted_command,
            description=_preview(description or ""),
            env=env,
            grade=grade,
            session_key=session.chat_session_id,
            source="web",
            allow_session=allow_session,
            allow_permanent=allow_permanent,
            smart_denied=smart_denied,
        )
        from tools.approval import get_web_approval
        av = get_web_approval(approval_id)
        _push({
            "type": "chat:approval_pending",
            "approval_id": approval_id,
            "command": redacted_command,
            "description": _preview(description or ""),
            "env": env,
            "grade": grade,
            "timeout_at": (av or {}).get("timeout_at"),
        })
        return wait_web_approval(approval_id) or "timeout"

    return _cb


def _run_chat_turn(
    session: "ChatSession",
    message: str,
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """worker 线程：驱动一次 agent turn；事件经 call_soon_threadsafe 进队列。

    线程内绑定审批上下文（interactive + session key + thread-local approval
    callback），保证危险命令走 web 审批注册表而不是静默放行/拒绝。
    """
    from tools.approval import (
        reset_current_session_key,
        reset_hermes_interactive_context,
        set_current_session_key,
        set_hermes_interactive_context,
    )
    from tools.terminal_tool import set_approval_callback

    def _push(event: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
        except Exception:
            _log.debug("chat event push failed", exc_info=True)

    tk1 = tk2 = None
    try:
        tk1 = set_hermes_interactive_context(True)
        tk2 = set_current_session_key(session.chat_session_id)
        set_approval_callback(_approval_callback_factory(session, queue, loop))

        history = _load_conversation_history(session)
        if not session.title:
            session.title = _preview(message, 60)
            session.last_activity_at = time.time()

        def _stream_cb(text: str) -> None:
            _push({"type": "chat:delta", "text": text})

        def _tool_cb(event: dict) -> None:
            if event.get("type") == "tool_start":
                _push({
                    "type": "chat:tool",
                    "name": event.get("name", ""),
                    "input_summary": event.get("input_summary", ""),
                })
            elif event.get("type") == "tool_end":
                _push({
                    "type": "chat:tool_result",
                    "name": event.get("name", ""),
                    "output_summary": event.get("output_summary", ""),
                    "ok": bool(event.get("ok", True)),
                })

        try:
            result = session.agent.run_conversation(
                message,
                conversation_history=history,
                task_id=session.chat_session_id,
                stream_callback=_stream_cb,
                tool_callback=_tool_cb,
            )
        except Exception as exc:
            _log.warning("chat turn failed (session=%s): %s", session.chat_session_id, exc)
            _push({
                "type": "chat:error",
                "message": f"{type(exc).__name__}: {exc}",
            })
            return

        if not isinstance(result, dict):
            _push({"type": "chat:error", "message": "agent 返回异常结果"})
            return
        error = result.get("error")
        if error:
            _push({"type": "chat:error", "message": str(error)})
            return
        _push({
            "type": "chat:done",
            "final_response": result.get("final_response") or "",
            "api_calls": result.get("api_calls"),
            "completed": bool(result.get("completed", True)),
            "partial": bool(result.get("partial", False)),
            "interrupted": bool(result.get("interrupted", False)),
            "failed": bool(result.get("failed", False)),
        })
    finally:
        set_approval_callback(None)
        if tk2 is not None:
            reset_current_session_key(tk2)
        if tk1 is not None:
            reset_hermes_interactive_context(tk1)
        session.last_activity_at = time.time()
        session.busy = False
        session._turn_done.set()
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ("__done__", None))
        except Exception:
            pass


def _sse_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _evict_if_needed() -> None:
    """软上限淘汰：最久未活动的空闲会话（busy 中的不淘汰）。

    调用方必须已持有 ``_CHAT_LOCK``（非重入锁，禁止内部再 acquire）。"""
    if len(_CHAT_SESSIONS) <= _CHAT_SESSIONS_MAX:
        return
    idle = sorted(
        [s for s in _CHAT_SESSIONS.values() if not s.busy],
        key=lambda s: s.last_activity_at,
    )
    for s in idle[: max(0, len(_CHAT_SESSIONS) - _CHAT_SESSIONS_MAX)]:
        _CHAT_SESSIONS.pop(s.chat_session_id, None)


def _cancel_pending_approvals_for_session(chat_session_id: str) -> int:
    """批三十六：中断当前 turn 前，把本会话所有 pending web 审批标 denied。

    只清理挂起的审批（不改审批核心逻辑）：``deny_web_approval`` 置状态 +
    唤醒等待中的审批回调（``wait_web_approval`` 返回 "deny"）→ 工具按拒绝
    收尾 → 审批中心/铃铛不再出现该 pending（不会僵尸挂起到超时）。
    """
    try:
        from tools.approval import deny_web_approval, list_web_approvals
    except Exception:
        return 0
    cancelled = 0
    try:
        views, _total = list_web_approvals(status="pending", limit=200)
    except Exception:
        _log.debug("chat interrupt approval scan failed", exc_info=True)
        return 0
    for v in views:
        if str(v.get("session_key") or "") != chat_session_id:
            continue
        try:
            deny_web_approval(str(v["id"]), reason="interrupted")
            cancelled += 1
        except Exception:
            _log.debug("chat interrupt approval cancel failed", exc_info=True)
    return cancelled


router = APIRouter()


@router.post("/api/chat/sessions")
async def create_chat_session():
    """创建会话：初始化 agent 实例并注册进进程内注册表。返回 {chat_session_id}。"""
    with _CHAT_LOCK:
        _evict_if_needed()
        session = ChatSession(chat_session_id=f"chat_{secrets_hex()}", agent=None, session_db=None)
        _CHAT_SESSIONS[session.chat_session_id] = session
    try:
        agent, session_db = await asyncio.to_thread(
            _build_chat_agent_pair, session.chat_session_id
        )
    except Exception as exc:
        with _CHAT_LOCK:
            _CHAT_SESSIONS.pop(session.chat_session_id, None)
        _log.warning("chat session agent init failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "agent_init_failed", "message": f"agent 初始化失败: {exc}"}},
        )
    session.agent = agent
    session.session_db = session_db
    return {"chat_session_id": session.chat_session_id, "created_at": session.created_at}


def _build_chat_agent_pair(chat_session_id: str):
    """返回 (agent, session_db) —— to_thread 包装便于异步端点不阻塞事件循环。"""
    agent = _create_chat_agent(chat_session_id)
    return agent, getattr(agent, "_session_db", None)


@router.get("/api/chat/sessions")
async def list_chat_sessions():
    """列出注册表中活会话（id + 标题预览 + 创建时间 + busy）。"""
    with _CHAT_LOCK:
        sessions = [s.view() for s in sorted(
            _CHAT_SESSIONS.values(), key=lambda s: s.last_activity_at, reverse=True
        )]
    return {"sessions": sessions, "total": len(sessions)}


@router.get("/api/chat/sessions/{chat_session_id}/messages")
async def chat_session_messages(chat_session_id: str):
    """拉取会话历史消息（结构化，供前端切回/切页恢复现场）。

    复用 ``_load_conversation_history``（SessionDB 持久化消息，verbatim 转录
    + 稳定行 id），返回对齐前端 ChatMessage 的字段
    （role/content/tools/timestamp，内容过 redact）；会话不在注册表 → 404。
    ``busy`` 随注册表状态返回（前端据此显示"处理中"）。
    """
    with _CHAT_LOCK:
        session = _CHAT_SESSIONS.get(chat_session_id)
        busy = bool(session.busy) if session is not None else False
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"会话不存在: {chat_session_id}"}},
        )
    history = _load_conversation_history(session, repair_alternation=False, include_row_ids=True)
    messages = _history_to_view_messages(history)
    return {
        "chat_session_id": chat_session_id,
        "messages": messages,
        "total": len(messages),
        "busy": busy,
    }


@router.post("/api/chat/sessions/{chat_session_id}/messages")
async def chat_message(chat_session_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    """发消息：SSE 流（chat:delta/tool/tool_result/approval_pending/done/error）。

    同会话串行：agent 忙时 409（前端禁用输入直到 done）。流结束即 turn 完成；
    agent 实例保留在注册表，下轮上下文由实例内状态 + SessionDB 历史续。
    """
    with _CHAT_LOCK:
        session = _CHAT_SESSIONS.get(chat_session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"会话不存在: {chat_session_id}"}},
        )
    if session.agent is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "agent_not_ready", "message": "会话 agent 初始化未完成"}},
        )
    message = str((payload or {}).get("message") or "").strip()
    if not message:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": "message 必填"}},
        )
    with _CHAT_LOCK:
        if session.busy:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "busy", "message": "会话正在处理上一条消息，请稍候"}},
            )
        session.busy = True
        session._turn_done.clear()

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(
        target=_run_chat_turn,
        args=(session, message, queue, loop),
        daemon=True,
        name=f"chat-turn-{chat_session_id[:16]}",
    ).start()

    async def _stream():
        try:
            while True:
                kind, data = await queue.get()
                if kind == "__done__":
                    break
                yield _sse_event(data.get("type", "message"), data)
        finally:
            # 客户端断开：等待 worker 完成（有限轮询，不阻塞事件循环太久）。
            deadline = time.monotonic() + 5.0
            while not session._turn_done.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers=dict(_SSE_HEADERS),
    )


@router.post("/api/chat/sessions/{chat_session_id}/interrupt")
async def chat_interrupt(chat_session_id: str):
    """中断当前 turn（对话页"停止"按钮；批三十六）。

    复用 AIAgent.interrupt()（gateway /stop / CLI Ctrl+C 同套机制），
    ``hard_cancel=True`` 表示显式停止（非 redirect/新消息打断）：
      - 会话不存在 → 404；不 busy → 409（重复中断/空闲都是明确状态）。
      - busy → 先取消本会话挂起审批（deny 唤醒等待中的工具），再 interrupt；
        等 worker 收尾（``_turn_done``，上限 5s）后返回 200。
    SSE 事件类型零新增：worker 侧 run_conversation 返回 interrupted → 现有
    chat:done 带 ``interrupted: true`` 收尾（或 chat:error）。
    """
    with _CHAT_LOCK:
        session = _CHAT_SESSIONS.get(chat_session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"会话不存在: {chat_session_id}"}},
        )
    if session.agent is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "agent_not_ready", "message": "会话 agent 初始化未完成"}},
        )
    with _CHAT_LOCK:
        if not session.busy:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "not_busy", "message": "会话当前没有进行中的操作"}},
            )
    cancelled = _cancel_pending_approvals_for_session(chat_session_id)
    try:
        session.agent.interrupt(hard_cancel=True)
    except Exception:
        _log.debug("chat interrupt failed (session=%s)", chat_session_id, exc_info=True)
    # 等 worker 收尾（busy 由 worker finally 复位，单一写入方不竞争）。
    deadline = time.monotonic() + 5.0
    while not session._turn_done.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return {
        "status": "interrupted",
        "chat_session_id": chat_session_id,
        "approvals_cancelled": cancelled,
    }


def clear_chat_sessions() -> None:
    """测试/进程内清理用。"""
    with _CHAT_LOCK:
        _CHAT_SESSIONS.clear()
