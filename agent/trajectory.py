"""Trajectory saving utilities and static helpers.

_convert_to_trajectory_format stays as an AIAgent method (batch_runner.py
calls agent._convert_to_trajectory_format). Only the static helpers and
the file-write logic live here.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def convert_scratchpad_to_think(content: str) -> str:
    """Convert <REASONING_SCRATCHPAD> tags to <think> tags."""
    if not content or "<REASONING_SCRATCHPAD>" not in content:
        return content
    return content.replace("<REASONING_SCRATCHPAD>", "<think>").replace("</REASONING_SCRATCHPAD>", "</think>")


def has_incomplete_scratchpad(content: str) -> bool:
    """Check if content has an opening <REASONING_SCRATCHPAD> without a closing tag."""
    if not content:
        return False
    return "<REASONING_SCRATCHPAD>" in content and "</REASONING_SCRATCHPAD>" not in content


def save_trajectory(trajectory: List[Dict[str, Any]], model: str,
                    completed: bool, filename: str = None):
    """Append a trajectory entry to a JSONL file.

    Args:
        trajectory: The ShareGPT-format conversation list.
        model: Model name for metadata.
        completed: Whether the conversation completed successfully.
        filename: Override output filename. Defaults to trajectory_samples.jsonl
                  or failed_trajectories.jsonl based on ``completed``.
    """
    if filename is None:
        filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"

    entry = {
        "conversations": trajectory,
        "timestamp": datetime.now().isoformat(),
        "model": model,
        "completed": completed,
    }

    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Trajectory saved to %s", filename)
    except Exception as e:
        logger.warning("Failed to save trajectory: %s", e)


# ===========================================================================
# 批次二十三：事件级运行轨迹（append-only session event log）
#
# 审计合规 + 事故复盘：每次工具调用（terminal 命令/审批/中断/错误）落一条
# 事件到 ~/.vigil/trajectory/<session_id>.jsonl，供 ``vigil trajectory``
# 查询与回放。与现有 save_trajectory（会话结束时的 ShareGPT conversation
# dump）并行——事件通道是新增通道，不替代既有格式。
#
# 设计（DSH SessionEvent log 借鉴，轻量版）：
#   - append-only：会话内只追加，不重写；seq 会话内递增（启动时读一次文件
#     行数作为基数，跨进程续号）。
#   - 强制 redact：action/result 过 redact_sensitive_text(credential_values=
#     True, force=True)——凭据值绝不落轨迹（硬约束 4）。
#   - 容量控制：单 session 上限 MAX_EVENTS_PER_SESSION，超限记一条
#     trajectory_truncated 后该 session 停止记录（防 runaway 会话撑爆磁盘）。
#   - best-effort：任何失败只记日志，绝不干扰执行路径。
# ===========================================================================

import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# 单 session 事件数上限（测试可 monkeypatch 覆写为小值）。
MAX_EVENTS_PER_SESSION = 5000
_TRAJECTORY_DIR_NAME = "trajectory"

# 进程内 per-session 状态：已写事件数（含启动时从文件读回的基数）与已截断
# 集合。锁保护：agent 工具线程与审批线程可能并发调用。
_trajectory_lock = threading.Lock()
_session_event_counts: Dict[str, int] = {}
_truncated_sessions = set()


def get_trajectory_dir() -> Path:
    """轨迹事件目录：<数据根>/trajectory/（随 VIGIL_HOME/profile 语义）。"""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).expanduser() / _TRAJECTORY_DIR_NAME


def _event_file_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")
    return get_trajectory_dir() / f"{safe[:120]}.jsonl"


def _read_existing_count(path: Path) -> int:
    """跨进程续号：append-only 文件已存在时读一次行数作为 seq 基数。"""
    try:
        if not path.is_file():
            return 0
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def _write_event_locked(path: Path, event: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as _exc:
        logger.warning("Failed to write trajectory event %s: %s", path, _exc)


def record_event(*, type: str, session_id: Optional[str] = None,
                 tool: Optional[str] = None, action: Optional[str] = None,
                 result: Optional[str] = None, approval: Optional[str] = None,
                 meta: Optional[Dict[str, Any]] = None) -> None:
    """Append one event to the session trajectory log (best-effort, never raises).

    Args:
        type: 事件类型——tool_call / tool_result / approval / interrupt /
            error / trajectory_truncated。
        session_id: 会话标识（terminal 用 session_id/task_id；approval 用
            get_current_session_key()）。None → "unknown"。
        tool: 工具名（terminal / topo_query / runbook_load / sudo_exec ...）。
        action: 命令/操作摘要（terminal 存 command、topo_query 存 query、
            runbook_load 存 name、sudo_exec 存 command）——强制 redact。
        result: 结果摘要（ok/error/exit_code/输出前 200 字符）——强制 redact。
        approval: 审批状态（requested / approved / denied / timeout / wait）。
        meta: 可选 ops 上下文（target host/env/grade 等；调用方保证不含凭据值，
            事件通道不再次 redact dict 内部值）。

    事件字段：ts / session_id / seq / type / tool / action / result /
    approval / meta。action 与 result 强制过 redact_sensitive_text
    （credential_values=True, force=True）——凭据值绝不落轨迹。
    单 session 超 MAX_EVENTS_PER_SESSION 条 → 记一条 trajectory_truncated
    后停止记录。
    """
    from agent.redact import redact_sensitive_text

    session_id = (session_id or "unknown").strip() or "unknown"
    path = _event_file_path(session_id)
    with _trajectory_lock:
        if session_id in _truncated_sessions:
            return
        count = _session_event_counts.get(session_id)
        if count is None:
            count = _read_existing_count(path)
        seq = count + 1
        if seq > MAX_EVENTS_PER_SESSION:
            _truncated_sessions.add(session_id)
            _write_event_locked(path, {
                "ts": _now_iso(),
                "session_id": session_id,
                "seq": seq,
                "type": "trajectory_truncated",
                "result": (
                    f"session event limit {MAX_EVENTS_PER_SESSION} reached; "
                    "recording stopped for this session"
                ),
            })
            logger.warning(
                "Trajectory truncated for session %s at %d events",
                session_id, MAX_EVENTS_PER_SESSION,
            )
            return
        _session_event_counts[session_id] = seq
        event: Dict[str, Any] = {
            "ts": _now_iso(),
            "session_id": session_id,
            "seq": seq,
            "type": type,
        }
        if tool:
            event["tool"] = tool
        if action:
            event["action"] = redact_sensitive_text(
                str(action), credential_values=True, force=True,
            )
        if result:
            event["result"] = redact_sensitive_text(
                str(result), credential_values=True, force=True,
            )
        if approval:
            event["approval"] = approval
        if meta:
            event["meta"] = meta
        _write_event_locked(path, event)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def reset_trajectory_state_for_tests() -> None:
    """测试用：清空进程内 per-session 计数/截断状态。"""
    with _trajectory_lock:
        _session_event_counts.clear()
        _truncated_sessions.clear()
