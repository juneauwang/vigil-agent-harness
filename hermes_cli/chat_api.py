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
from datetime import datetime, timedelta, timezone
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
class _WebClarifyEntry:
    """批四十九：web chat 挂起 clarify（session 级，一次一个）。

    形状对齐审批的挂起条目：线程内回调登记 → SSE chat:clarify_pending 推给
    前端 → 回调线程阻塞在 threading.Event 上 → 前端 POST
    /api/chat/sessions/{id}/clarify 写入 response 并 set 事件 → 回调返回。
    状态挂在 ChatSession.pending_clarify 上：多 session 并行各自独立，不
    用模块级全局表互踩（与 gateway 的 clarify_gateway 按 session_key 索引
    同语义，只是 web 会话天然以 ChatSession 为容器）。
    """
    clarify_id: str
    question: str
    choices: Optional[List[str]]
    multi_select: bool
    timeout_at: Optional[str]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None

    def view(self) -> dict:
        return {
            "clarify_id": self.clarify_id,
            "question": self.question,
            "choices": list(self.choices) if self.choices else None,
            "multi_select": bool(self.multi_select),
            "timeout_at": self.timeout_at,
        }


@dataclass
class ChatSession:
    chat_session_id: str
    agent: Any
    session_db: Any
    created_at: str = field(default_factory=_now_iso_utc)
    title: str = ""
    last_activity_at: float = field(default_factory=time.time)
    busy: bool = False
    # 批四十一 §8：会话级模型（空 = 配置默认；创建/切换时写入，agent 已按
    # 该模型构建/切换，保留一份给列表视图展示）。
    model: str = ""
    # 最近一次审批的终态（"approval_timeout" / "denied" / None）——批三十八
    # 审批超时/拒绝路径 finalize 用。审批核心逻辑不动，只在本会话层记录结果。
    last_approval_outcome: Optional[str] = None
    # 批四十九：当前挂起的 web clarify（None = 无）。一次 turn 内至多一个。
    pending_clarify: Optional[_WebClarifyEntry] = None
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
            "model": self.model,
        }


def _create_chat_agent(chat_session_id: str, model: Optional[str] = None):
    """构造会话 agent（mirror oneshot 的非交互路径；平台标记 web）。

    模型/运行时照 config.yaml model.* + resolve_runtime_provider；工具集取
    platform_toolsets.cli（用户经 ``vigil tools`` 的既有配置）；MCP 在构造前
    幂等发现；会话历史落 SQLite SessionDB（session_id = chat_session_id）。
    批四十一 §8：``model`` 可选覆盖（会话级模型，缺省 = 配置默认）。
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
    cfg_default = str(cfg_model or "").strip()
    effective_model = str(model or "").strip() or cfg_default
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
                    # 批四十一 §5：历史工具行保留服务端 tool_call id（有则
                    # 有，无则空串——按顺序正序挂接已能保证同名单工具不错位）。
                    "tool_id": str(tc.get("id") or tc.get("tool_call_id") or ""),
                })
            reasoning_text = _redact_text(
                (msg.get("reasoning") or msg.get("reasoning_content") or "")
            )
            # 批四十二 §BJ：历史推理结构保留向后兼容——0/多工具调用或空推理
            # 输出旧单值字符串；恰一个带 id 工具调用时结构化归属该步
            # {steps: [{tool_id, text}]}，前端按 tool_id 挂回对应工具行。
            reasoning_view: Any = reasoning_text
            if reasoning_text:
                tc_ids = [
                    str(tc.get("id") or tc.get("tool_call_id") or "")
                    for tc in (msg.get("tool_calls") or [])
                ]
                if len(tc_ids) == 1 and tc_ids[0]:
                    reasoning_view = {"steps": [{"tool_id": tc_ids[0], "text": reasoning_text}]}
            view.append({
                "id": msg_id if msg_id is not None else len(view) + 1,
                "role": "assistant",
                "content": _redact_text(msg.get("content") or ""),
                # 批四十一 §3：历史消息带 reasoning（模型 reasoning 字段或
                # reasoning_content，纯文本拼接；前端默认折叠展示）。
                "reasoning": reasoning_view,
                "tools": tools,
                "timestamp": msg.get("timestamp"),
            })
            pending_tools = tools
        elif role == "tool":
            content = _preview(msg.get("content") or "")
            name = str(msg.get("tool_name") or "")
            tcid = str(msg.get("tool_call_id") or "")
            matched = False
            # 优先 tool_call_id 精确挂接；否则按工具调用顺序正序匹配——工具
            # 结果行恒以 tool_calls 顺序落库（并行执行也按原序收齐追加），
            # 正序匹配保证同名单并行工具输出零错位。
            for t in pending_tools:
                if t.get("output_summary") is not None:
                    continue
                if tcid and t.get("tool_id") == tcid:
                    t["output_summary"] = content
                    t["ok"] = True
                    matched = True
                    break
            if not matched:
                for t in pending_tools:
                    if t.get("output_summary") is not None:
                        continue
                    if not name or t.get("name") == name:
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
        _log.info("[%s] approval gate fired: cmd=%r desc=%r",
                  session.chat_session_id, command[:80], (description or "")[:80])
        env = _default_ops_env()
        grade = _probe_ops_grade(command)
        redacted_command = _redact_text(command)
        approval_id = register_web_approval(
            command=redacted_command,
            # 批四十一 §6：description 存全量（调用方已 redact），前端可展开
            # 完整详情，不再单向截断丢信息。
            description=_redact_text(description or ""),
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
        _log.info("[%s] approval registered: id=%s status=%s", session.chat_session_id, approval_id, (av or {}).get("status"))
        _push({
            "type": "chat:approval_pending",
            "approval_id": approval_id,
            "command": redacted_command,
            "description": _redact_text(description or ""),
            "env": env,
            "grade": grade,
            "timeout_at": (av or {}).get("timeout_at"),
        })
        remaining = _approval_remaining_seconds(av)
        if remaining is not None and remaining <= 0:
            session.last_approval_outcome = "approval_timeout"
            return "timeout"
        choice = wait_web_approval(approval_id, timeout=remaining)
        choice = choice or "timeout"
        if choice == "timeout":
            session.last_approval_outcome = "approval_timeout"
        elif choice == "deny":
            session.last_approval_outcome = "denied"
        return choice

    return _cb


def _approval_remaining_seconds(view: Optional[dict]) -> Optional[float]:
    """审批剩余等待秒数（由 timeout_at 推导；缺字段/解析失败 → None 表示不限时）。

    批三十八：web 审批的 wait 语义本身不唤醒等待方（reclaim 只标记 timeout），
    会话层在这里自限等待窗口，超时返回 "timeout" 让 turn 走 fail-closed 收尾，
    而不是无限阻塞卡死整个 session（§AW）。审批核心逻辑零改动。
    """
    if not view:
        return None
    timeout_at = view.get("timeout_at")
    if not timeout_at:
        return None
    try:
        deadline = datetime.fromisoformat(str(timeout_at).replace("Z", "+00:00"))
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        return None


def _clarify_callback_factory(session: "ChatSession", queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """web 会话 clarify 回调（线程内调用，仿 _approval_callback_factory）。

    LLM 调 clarify（tools/clarify_tool 语义 question/choices/multi_select，
    工具定义零改动）→ 本回调登记挂起条目 → SSE ``chat:clarify_pending`` 推送
    （session_id/question/choices/multi_select/timeout_at）→ 回调线程阻塞在
    threading.Event 上 → 前端 POST /api/chat/sessions/{id}/clarify 写入应答并
    set 事件 → 回调返回选择串。超时（clarify_timeout，config_defaults 已有，
    不新增配置）→ 返回 ``[user did not respond within Xm]``，agent 自行决定。

    状态挂在 ``session.pending_clarify``（多 session 并行各自独立，无全局表
    互踩）；answer 不回显不落日志——敏感答复（sudo 密码等）沿用批三十二
    redact 机制在持久化/展示层精确打码。
    """
    from tools.clarify_gateway import get_clarify_timeout

    def _push(event: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
        except Exception:
            _log.debug("chat clarify event push failed", exc_info=True)

    def _cb(question: str, choices, multi_select: bool = False) -> str:
        try:
            timeout_s = float(get_clarify_timeout() or 3600)
        except Exception:
            timeout_s = 3600.0
        deadline = None
        if timeout_s > 0:
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_s)
        entry = _WebClarifyEntry(
            clarify_id=f"clfy_{secrets_hex()}",
            question=_redact_text(question or ""),
            choices=[str(c) for c in (choices or [])] or None,
            multi_select=bool(multi_select) and bool(choices),
            timeout_at=deadline.strftime("%Y-%m-%dT%H:%M:%SZ") if deadline else None,
        )
        session.pending_clarify = entry
        _push({
            "type": "chat:clarify_pending",
            "session_id": session.chat_session_id,
            **entry.view(),
        })
        # 等应答：1s 切片轮询 deadline（对齐 gateway wait_for_response 的
        # 分片语义；web 无 activity watchdog，分片只为超时及时返回）。
        while entry.response is None and not entry.event.is_set():
            if entry.timeout_at is None:
                entry.event.wait(timeout=1.0)
                continue
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            entry.event.wait(timeout=min(1.0, remaining))
        session.pending_clarify = None
        if entry.response is not None:
            return entry.response
        minutes = max(1, int(timeout_s / 60))
        return f"[user did not respond within {minutes}m]"

    return _cb


def _cancel_pending_clarify_for_session(session: "ChatSession") -> bool:
    """批四十九：中断当前 turn 前把本会话挂起 clarify 解挂（对齐审批 deny 唤醒）。

    只解挂挂起条目（不改 clarify 工具/超时逻辑）：置空应答 + set 事件 →
    阻塞中的 clarify 回调立刻返回，agent 线程继续收 interrupt 收尾，不会
    僵尸挂到超时。
    """
    entry = getattr(session, "pending_clarify", None)
    if entry is None:
        return False
    entry.response = ""
    entry.event.set()
    session.pending_clarify = None
    return True


def _live_session_id(session: "ChatSession") -> str:
    """会话行 id：压缩后 agent.session_id 会轮转到压缩子行，finalize/复活必须
    打活行而不是对话创建时的 chat_session_id（批三十八）。"""
    agent = getattr(session, "agent", None)
    live = str(getattr(agent, "session_id", None) or "")
    return live or session.chat_session_id


def _ensure_session_running(session: "ChatSession") -> None:
    """turn 起点：把会话行状态复位为 running（上一 turn 收尾已落终态）。"""
    session_db = getattr(session, "session_db", None)
    if session_db is None:
        return
    try:
        session_db.set_session_status(_live_session_id(session), "running")
    except Exception:
        _log.debug("chat turn start status reset skipped (session=%s)", session.chat_session_id, exc_info=True)


def _finalize_turn_session(session: "ChatSession", *, interrupted: bool, failed: bool) -> None:
    """turn 收尾统一入口：会话行落显式终态（批三十八）。

    优先级：interrupt > 审批超时/拒绝 > failed/error > 正常结束。落库失败时
    SessionDB.finalize_session_row 内部兜底打 finalize_error，这里只记日志。
    """
    session_db = getattr(session, "session_db", None)
    if session_db is None:
        return
    approval_outcome = getattr(session, "last_approval_outcome", None)
    if interrupted:
        status, reason = "interrupted", "interrupted"
    elif approval_outcome in ("approval_timeout", "denied"):
        status, reason = "ended", approval_outcome
    elif failed:
        status, reason = "ended", "error"
    else:
        status, reason = "ended", "turn_complete"
    try:
        session_db.finalize_session_row(
            _live_session_id(session),
            status=status,
            end_reason=reason,
        )
    except Exception:
        _log.warning(
            "chat turn finalize failed (session=%s status=%s reason=%s)",
            session.chat_session_id, status, reason,
            exc_info=True,
        )
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
    turn_interrupted = False
    turn_failed = False
    try:
        tk1 = set_hermes_interactive_context(True)
        tk2 = set_current_session_key(session.chat_session_id)
        set_approval_callback(_approval_callback_factory(session, queue, loop))
        # 批四十九：web chat clarify 回调接线（此前缺失 → LLM 调 clarify 只会
        # 拿到 "Clarify tool is not available"，已知 bug）。会话级挂起条目 +
        # SSE chat:clarify_pending + POST /clarify 应答端点。
        session.pending_clarify = None
        session.agent.clarify_callback = _clarify_callback_factory(session, queue, loop)
        session.last_approval_outcome = None

        history = _load_conversation_history(session)
        if not session.title:
            session.title = _preview(message, 60)
            session.last_activity_at = time.time()
        _ensure_session_running(session)

        def _stream_cb(text: str) -> None:
            _push({"type": "chat:delta", "text": text})

        # 批四十一 §5：工具事件携带服务端唯一 tool_id（模型 tool_call id；
        # 提供商缺 id 时按本次 turn 内工具到达顺序补一个序号兜底），前端按
        # id 挂接输出——并行同名单工具调用不再错位挂接。
        tool_seq: list[int] = [0]
        tool_ids: list[str] = []

        # 批四十二 §BJ：推理增量先入缓冲——模型推理先于工具执行到达，此时
        # 还不知道该段推理会导向哪个 tool_call；待 tool_start 再按 tool_id
        # 归属转发（chat:tool 先推，前端先建工具行再挂推理）。未被任何工具
        # 认领的推理（如最终答复前的思考）在收尾事件前按消息级转发。
        pending_reasoning: list[str] = []

        def _flush_reasoning(tool_id: str = "") -> None:
            if not pending_reasoning:
                return
            text = "".join(pending_reasoning)
            pending_reasoning.clear()
            if not text:
                return
            ev: Dict[str, Any] = {"type": "chat:reasoning", "text": text}
            if tool_id:
                ev["tool_id"] = tool_id
            _push(ev)

        def _tool_cb(event: dict) -> None:
            tool_id = str(event.get("tool_id") or "")
            if event.get("type") == "tool_start":
                if not tool_id:
                    tool_seq[0] += 1
                    tool_id = f"tool-{tool_seq[0]}"
                tool_ids.append(tool_id)
                _push({
                    "type": "chat:tool",
                    "tool_id": tool_id,
                    "name": event.get("name", ""),
                    "input_summary": event.get("input_summary", ""),
                })
                _flush_reasoning(tool_id)
            elif event.get("type") == "tool_end":
                # 批三十八 §AS A4：工具/API 完成即刷新活动时间，不只在 turn
                # 起点写一次（注册表排序/停止轮询据此判断进度）。
                session.last_activity_at = time.time()
                if not tool_id and tool_ids:
                    tool_id = tool_ids.pop(0)
                _push({
                    "type": "chat:tool_result",
                    "tool_id": tool_id,
                    "name": event.get("name", ""),
                    "output_summary": event.get("output_summary", ""),
                    "ok": bool(event.get("ok", True)),
                })

        def _reasoning_cb(text: str) -> None:
            pending_reasoning.append(text)

        # 批四十一 §3：推理过程增量事件（模型输出 reasoning 时转发，默认不
        # 出——chat:reasoning 事件仅供前端折叠展示，不参与对话上下文）。
        session.agent.reasoning_callback = _reasoning_cb

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
            turn_failed = True
            _flush_reasoning()
            _push({
                "type": "chat:error",
                "message": f"{type(exc).__name__}: {exc}",
            })
            return

        if not isinstance(result, dict):
            turn_failed = True
            _flush_reasoning()
            _push({"type": "chat:error", "message": "agent 返回异常结果"})
            return
        error = result.get("error")
        if error:
            turn_failed = True
            _flush_reasoning()
            _push({"type": "chat:error", "message": str(error)})
            return
        turn_interrupted = bool(result.get("interrupted", False))
        turn_failed = bool(result.get("failed", False))
        _flush_reasoning()
        _push({
            "type": "chat:done",
            "final_response": result.get("final_response") or "",
            "api_calls": result.get("api_calls"),
            "completed": bool(result.get("completed", True)),
            "partial": bool(result.get("partial", False)),
            "interrupted": turn_interrupted,
            "failed": turn_failed,
        })
    finally:
        try:
            session.agent.reasoning_callback = None
        except Exception:
            pass
        try:
            session.agent.clarify_callback = None
        except Exception:
            pass
        session.pending_clarify = None
        set_approval_callback(None)
        if tk2 is not None:
            reset_current_session_key(tk2)
        if tk1 is not None:
            reset_hermes_interactive_context(tk1)
        _finalize_turn_session(
            session,
            interrupted=turn_interrupted,
            failed=turn_failed,
        )
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


def _model_catalog() -> dict:
    """批四十一 §8：会话可选模型目录（静态，零网络、零敏感信息）。

    返回 {"models": [...], "provider": ..., "default_model": ...}。来源：
    配置 model.default（始终在列、标 default）+ 静态目录（提供商的
    _PROVIDER_MODELS / OpenRouter / Vercel AI Gateway 快照）。标注 tag 由
    目录描述或模型名派生（快/省 vs 强/慢），纯展示不做准确承诺。
    """
    from hermes_cli.models import (
        OPENROUTER_MODELS,
        VERCEL_AI_GATEWAY_MODELS,
        _PROVIDER_MODELS,
    )

    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        cfg = {}
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        default_model = str(model_cfg).strip()
    else:
        default_model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    provider = str(model_cfg.get("provider") or "").strip() or "auto"

    desc: dict = {}
    if provider == "openrouter":
        for mid, d in OPENROUTER_MODELS:
            desc[mid] = d
    elif provider in ("ai-gateway", "vercel"):
        for mid, d in VERCEL_AI_GATEWAY_MODELS:
            desc[mid] = d

    try:
        from hermes_cli.models import normalize_provider
        provider_key = normalize_provider(provider) or provider
    except Exception:
        provider_key = provider
    catalog: list[str] = []
    for key in (provider_key, provider):
        for mid in _PROVIDER_MODELS.get(key, []):
            if mid not in catalog:
                catalog.append(mid)
    # 聚合器目录作为补充（OpenRouter 快照本身就有描述）。
    for mid, _d in desc.items():
        if mid not in catalog:
            catalog.append(mid)

    known = set(catalog) | set(desc)
    if default_model and default_model not in known:
        known.add(default_model)

    def _tag(mid: str) -> str:
        d = desc.get(mid, "")
        low = mid.lower()
        desc_low = d.lower()
        if (
            "mini" in low or "flash" in low or "lite" in low or "haiku" in low
            or "fast" in low or "nano" in low or "compact" in low
            or "cheap" in desc_low or "fast" in desc_low or "省" in desc_low or "快" in desc_low
        ):
            return "快/省"
        if (
            "opus" in low or "pro" in low or "max" in low or "sol" in low
            or "ultra" in low or "reasoning" in low or "strong" in desc_low or "强" in desc_low
        ):
            return "强/慢"
        return ""

    # 目录顺序：配置默认置顶，其余按出现顺序。
    ordered = [default_model] if default_model else []
    for mid in catalog + [m for m in known if m not in catalog]:
        if mid not in ordered:
            ordered.append(mid)
    models = [{
        "id": mid,
        "name": mid,
        "description": desc.get(mid, ""),
        "tag": _tag(mid),
        "default": mid == default_model,
    } for mid in ordered if mid]
    return {"models": models, "provider": provider, "default_model": default_model}


@router.get("/api/models")
async def list_models():
    """批四十一 §8：会话可选模型目录（静态目录 + 配置默认；零网络/敏感信息）。
    前端选择器选项严格来自该接口。"""
    return _model_catalog()


@router.post("/api/chat/sessions")
async def create_chat_session(payload: Dict[str, Any] = Body(default_factory=dict)):
    """创建会话：初始化 agent 实例并注册进进程内注册表。返回 {chat_session_id}。

    批四十一 §8：body 可选 {model}——会话级模型（缺省用配置默认）。模型必须
    在 /api/models 目录内（配置外模型不可选）。
    """
    model = str((payload or {}).get("model") or "").strip() or None
    if model:
        catalog = {m["id"] for m in _model_catalog()["models"]}
        if model not in catalog:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_request", "message": f"模型不在可选目录: {model}"}},
            )
    with _CHAT_LOCK:
        _evict_if_needed()
        session = ChatSession(
            chat_session_id=f"chat_{secrets_hex()}",
            agent=None,
            session_db=None,
            model=model or "",
        )
        _CHAT_SESSIONS[session.chat_session_id] = session
    try:
        agent, session_db = await asyncio.to_thread(
            _build_chat_agent_pair, session.chat_session_id, model
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
    return {
        "chat_session_id": session.chat_session_id,
        "created_at": session.created_at,
        "model": session.model or _model_catalog()["default_model"],
    }


def _build_chat_agent_pair(chat_session_id: str, model: Optional[str] = None):
    """返回 (agent, session_db) —— to_thread 包装便于异步端点不阻塞事件循环。"""
    agent = _create_chat_agent(chat_session_id, model=model)
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
    # 批四十九：解挂本会话挂起 clarify（阻塞中的回调立刻返回，随 interrupt
    # 一起收尾），否则 clarify 会僵尸挂到超时。
    _cancel_pending_clarify_for_session(session)
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


@router.post("/api/chat/sessions/{chat_session_id}/clarify")
async def chat_clarify_answer(chat_session_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
    """批四十九：web chat clarify 应答端点。

    body ``{answer: str | str[]}``（多选时前端传数组；空串 = 用户"取消/跳过"，
    agent 回合继续）。语义对齐 approval：会话不存在 → 404；无挂起 clarify →
    409（no_pending_clarify）；已超时 → 409（clarify_timed_out，前端显示
    "已超时，agent 自行决定"）。

    安全：answer 不回显、不写日志——敏感答复（sudo 密码等）只经批三十二
    redact 机制在持久化/展示层精确打码（clarify_tool 登记 + _redact_text 展示）。
    """
    with _CHAT_LOCK:
        session = _CHAT_SESSIONS.get(chat_session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"会话不存在: {chat_session_id}"}},
        )
    with _CHAT_LOCK:
        entry = session.pending_clarify
    if entry is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "no_pending_clarify", "message": "当前没有等待回答的 clarify"}},
        )
    if entry.timeout_at is not None:
        try:
            deadline = datetime.fromisoformat(str(entry.timeout_at).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) >= deadline:
                return JSONResponse(
                    status_code=409,
                    content={"error": {"code": "clarify_timed_out", "message": "clarify 已超时，agent 已自行决定"}},
                )
        except ValueError:
            pass
    body = payload or {}
    if "answer" not in body:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": "answer 必填"}},
        )
    answer = body.get("answer")
    if isinstance(answer, list):
        cleaned = [str(a).strip() for a in answer]
    else:
        cleaned = str(answer).strip() if answer is not None else ""
    with _CHAT_LOCK:
        entry.response = cleaned
        entry.event.set()
    return {
        "status": "resolved",
        "clarify_id": entry.clarify_id,
        "chat_session_id": chat_session_id,
    }


@router.post("/api/chat/sessions/{chat_session_id}/model")
async def chat_set_session_model(chat_session_id: str,
                                 payload: Dict[str, Any] = Body(default_factory=dict)):
    """批四十一 §8：切换会话模型（会话级生效，下一条消息起用新模型）。

    模型必须在 /api/models 目录内；忙时 409（避免改模型打断进行中的 turn）；
    会话不存在 404。切换复用 AIAgent.switch_model（runtime 照新模型解析）。
    """
    with _CHAT_LOCK:
        session = _CHAT_SESSIONS.get(chat_session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": f"会话不存在: {chat_session_id}"}},
        )
    model = str((payload or {}).get("model") or "").strip()
    if not model:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": "model 必填"}},
        )
    catalog = {m["id"] for m in _model_catalog()["models"]}
    if model not in catalog:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request", "message": f"模型不在可选目录: {model}"}},
        )
    with _CHAT_LOCK:
        if session.busy:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "busy", "message": "会话正在处理消息，请稍候再切换模型"}},
            )
        if session.agent is None:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "agent_not_ready", "message": "会话 agent 初始化未完成"}},
            )
        agent = session.agent
        if model == session.model:
            return {"chat_session_id": chat_session_id, "model": model}
        session.model = model
    try:
        await asyncio.to_thread(_switch_session_agent_model, agent, model)
    except Exception as exc:
        with _CHAT_LOCK:
            # 切换失败：回滚会话模型字段，避免视图与 agent 实际模型不一致。
            session.model = ""
        _log.warning("chat model switch failed (session=%s): %s", chat_session_id, exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "model_switch_failed", "message": f"模型切换失败: {exc}"}},
        )
    return {"chat_session_id": chat_session_id, "model": model}


def _switch_session_agent_model(agent, model: str) -> None:
    """按目标模型解析 runtime 并原地切换 agent（to_thread 包装）。"""
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(target_model=model or None)
    agent.switch_model(
        model,
        runtime.get("provider"),
        api_key=runtime.get("api_key") or "",
        base_url=runtime.get("base_url") or "",
        api_mode=runtime.get("api_mode") or "",
    )


def clear_chat_sessions() -> None:
    """测试/进程内清理用。"""
    with _CHAT_LOCK:
        _CHAT_SESSIONS.clear()
