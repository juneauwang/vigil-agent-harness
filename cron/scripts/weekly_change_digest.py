#!/usr/bin/env python3
"""Vigil 周期变更汇总采集器（确定性取数，不做叙述）。

链路定位::

    本脚本（取数 + 渲染） -> 模型（只补写摘要/风险叙述） -> publish_bookstack

设计约束（对应任务书 D1/D3）：

* 窗口统一左闭右开 ``[since, until)``，时区固定 Asia/Shanghai。
* 会话归属按 ``sessions.started_at`` 落在窗口内判定；消息级信息（写操作、
  错误、只读工具 Top）只统计窗口内的消息。
* 写操作判定**不使用** ``effect_disposition``（实测为 None/none/unknown）。
  terminal 调用走 ``tools/action_classifier.classify_command``；链式命令按
  ``chain`` 拆开，任一子命令命中写类即记一次写操作，全部识别不出才进
  ``unclassified``。
* 拓扑变更只能靠「本次快照 vs 上次快照」diff —— ``topo_update`` 没有落盘审计。
* 纯标准库 + pyyaml，禁止网络；只读打开 state.db。
* facts 里所有字符串都去换行并截断；不允许整段命令/文件内容进入 facts。

退出码：0 正常 / 2 参数错误 / 3 数据库打开失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

try:  # 仓库根加入 sys.path，兼容从 cron/scripts 直接运行
    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
except Exception:  # pragma: no cover
    pass

try:  # 项目内分类器（editable 安装，任何 cwd 都可导入）
    from tools.action_classifier import classify_command
except Exception as exc:  # pragma: no cover - 环境残缺时给出可读错误
    print(f"weekly_change_digest: 无法加载 tools.action_classifier: {exc}", file=sys.stderr)
    raise SystemExit(3)

try:
    import yaml
except Exception as exc:  # pragma: no cover - pyyaml 是既有依赖
    print(f"weekly_change_digest: 需要 pyyaml: {exc}", file=sys.stderr)
    raise SystemExit(3)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo

    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 无 tzdata 时固定 +08:00
    SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

# 写类动作（任务书 §3.5）。backup/query/fetch_log/verify 不算写。
WRITE_ACTIONS = frozenset({
    "start", "stop", "reload", "transfer_file", "restart", "install",
    "upgrade", "apply_config", "run_script", "scale", "deploy", "rollback",
    "restore", "remove", "decommission", "reboot", "shutdown",
})

# 链式命令命中多个写类动作时，取风险最高（静态档位）的那个作为展示动作。
_WRITE_PRIORITY = {
    "start": 3, "stop": 3, "reload": 3, "transfer_file": 3,
    "restart": 4,
    "install": 5, "upgrade": 5, "apply_config": 5,
    "run_script": 6, "scale": 6,
    "deploy": 7, "rollback": 7,
    "restore": 8,
    "remove": 9, "decommission": 9,
    "reboot": 10, "shutdown": 10,
}

# 非 terminal 的写类工具白名单（任务书 §3.5）。
NON_TERMINAL_WRITE_TOOLS = frozenset({
    "write_file", "patch", "topo_update", "skill_manage", "memory",
    "publish_bookstack",
})

# end_reason 里出现这些词视为未闭环/异常结束。
_BAD_END_TOKENS = ("interrupt", "timeout", "error", "fail", "crash", "abort", "cancel")
_GOOD_END_REASONS = frozenset({"turn_complete", "cli_close", "tui_close", "done", "completed", "complete", "closed", "user_exit", "agent_close"})

# 拓扑里不进入 facts 的字段（凭据类 / 过长备注另做截断）。
_TOPO_SKIP_FIELDS = frozenset({
    "credentials", "credential", "password", "passwd", "token", "secret",
    "api_key", "private_key",
})

MAX_FIRST_USER_MSG = 200
MAX_SUMMARY = 120
MAX_ERROR = 160
MAX_FIELD_VALUE = 200
MAX_UNCLASSIFIED = 50
MAX_ERRORS_PER_SESSION = 8
MAX_COMMITS_PER_REPO = 200
MAX_MD_RISK_LINES = 40

_WS_RE = re.compile(r"\s+")
_EMOJI_RE = re.compile(
    "[" 
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization|bearer)"
    r"\s*[=:]\s*(?!\*\*\*)\S+"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?\S+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")


# ---------------------------------------------------------------------------
# 通用文本处理
# ---------------------------------------------------------------------------

def clean_text(value: Any, limit: int) -> str:
    """去换行、压缩空白、截断到 ``limit`` 个字符。"""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def redact_text(text: str) -> str:
    """轻量脱敏：facts / 报告里不允许出现 token、密码类值。"""
    value = text or ""
    # 先处理 Authorization 头（``Bearer <token>``），避免只吞掉 scheme。
    value = _AUTH_HEADER_RE.sub(r"\1***", value)
    value = _BEARER_RE.sub("Bearer ***", value)
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", value)


def strip_emoji(text: str) -> str:
    """报告正文禁 emoji；facts 里的动态文本在渲染时清掉。"""
    return _EMOJI_RE.sub("", text or "")


def md_cell(value: Any) -> str:
    """Markdown 表格单元：禁 emoji、禁换行、转义竖线。"""
    text = strip_emoji(clean_text(value, MAX_FIELD_VALUE))
    return text.replace("|", "\\|")


def _iso(dt: datetime) -> str:
    return dt.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _iso_week_label(dt: datetime) -> str:
    iso = dt.astimezone(SHANGHAI).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _home_dir() -> Path:
    raw = os.environ.get("VIGIL_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".vigil"


# ---------------------------------------------------------------------------
# 窗口解析
# ---------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d+)\s*([dhw])$", re.IGNORECASE)
_WEEK_RE = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.IGNORECASE)


def _parse_point(raw: str) -> datetime:
    """解析 ``YYYY-MM-DD`` 或 ISO 8601 时间点；无时区按 Asia/Shanghai。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("空的时间参数")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, tzinfo=SHANGHAI)
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {raw!r}（支持 YYYY-MM-DD 或 ISO 8601）") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI)


def resolve_window(args: argparse.Namespace, now: Optional[datetime] = None) -> Dict[str, str]:
    """返回 facts.window: {since, until, label}。

    优先级：--week > --since/--until > --window（默认 7d）。
    所有窗口左闭右开；label 取窗口结束前最后一刻所在 ISO 周（--week 用其本身）。
    """
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    has_since = args.since is not None
    has_until = args.until is not None
    if has_since != has_until:
        raise ValueError("--since 与 --until 必须同时提供")

    if args.week:
        if has_since or args.window:
            raise ValueError("--week 不能与 --since/--until/--window 同时使用")
        match = _WEEK_RE.match(args.week.strip())
        if not match:
            raise ValueError(f"无法解析 ISO 周: {args.week!r}（形如 2026-W44）")
        year, week = int(match.group(1)), int(match.group(2))
        try:
            monday = date.fromisocalendar(year, week, 1)
        except ValueError as exc:
            raise ValueError(f"非法 ISO 周: {args.week!r}") from exc
        since = datetime(monday.year, monday.month, monday.day, tzinfo=SHANGHAI)
        until = since + timedelta(days=7)
        label = f"{year}-W{week:02d}"
    elif has_since:
        if args.window:
            raise ValueError("--since/--until 不能与 --window 同时使用")
        since = _parse_point(args.since)
        until = _parse_point(args.until)
        if since >= until:
            raise ValueError("--since 必须早于 --until")
        label = _iso_week_label(since)
    else:
        raw_window = (args.window or "7d").strip()
        match = _WINDOW_RE.match(raw_window)
        if not match:
            raise ValueError(f"无法解析窗口: {raw_window!r}（形如 7d / 24h / 2w）")
        amount, unit = int(match.group(1)), match.group(2).lower()
        if amount <= 0:
            raise ValueError("窗口长度必须大于 0")
        delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
        until = now
        since = until - delta
        label = _iso_week_label(since)

    return {"since": _iso(since), "until": _iso(until), "label": label}


# ---------------------------------------------------------------------------
# SQLite（只读）
# ---------------------------------------------------------------------------

def _open_db(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(db_path.resolve().as_posix())}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    # 快速失败：表不存在说明不是 Vigil 的 state.db。
    con.execute("SELECT 1 FROM sessions LIMIT 1").fetchall()
    con.execute("SELECT 1 FROM messages LIMIT 1").fetchall()
    return con


def _iter_tool_calls(raw: Any) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """从 messages.tool_calls JSON 里稳定产出 (tool_name, arguments)。"""
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return
    if not isinstance(calls, list):
        return
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        yield name, args


def _terminal_actions(command: str) -> List[str]:
    """terminal 命令 -> 各子命令动作列表（链式用 chain 拆开）。"""
    result = classify_command(command)
    chain = result.get("chain")
    if isinstance(chain, list) and chain:
        actions = [c.get("action") for c in chain if isinstance(c, dict)]
    else:
        actions = [result.get("action")]
    return [a for a in actions if isinstance(a, str) and a]


def _pick_write_action(actions: Sequence[str]) -> Optional[str]:
    writes = [a for a in actions if a in WRITE_ACTIONS]
    if not writes:
        return None
    return max(writes, key=lambda a: _WRITE_PRIORITY.get(a, 0))


def _summarize_args(tool: str, args: Dict[str, Any]) -> str:
    """非 terminal 写类工具的摘要：只取定位字段，绝不 dump 正文。"""
    for key in ("path", "file_path", "name", "entity", "action", "target"):
        value = args.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return clean_text(redact_text(str(value)), MAX_SUMMARY)
    keys = [k for k in args.keys() if k not in {"content", "new_str", "old_str", "markdown"}]
    return clean_text(redact_text(", ".join(str(k) for k in keys[:6])), MAX_SUMMARY) or tool


def _tool_error(content: Any) -> Optional[str]:
    """从 tool 结果里提取错误文本；没有错误返回 None。"""
    if content is None:
        return None
    text = str(content).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        if text.startswith("Tool execution failed") or "Traceback (most recent call last)" in text[:300]:
            return clean_text(redact_text(text), MAX_ERROR)
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if err:
        return clean_text(redact_text(str(err)), MAX_ERROR)
    if data.get("success") is False:
        return clean_text(
            redact_text(str(data.get("message") or data.get("output") or "success=false")),
            MAX_ERROR,
        )
    if data.get("status") == "error":
        return clean_text(
            redact_text(str(data.get("output") or data.get("message") or "status=error")),
            MAX_ERROR,
        )
    exit_code = data.get("exit_code")
    if isinstance(exit_code, (int, float)) and exit_code != 0:
        detail = data.get("output") or data.get("stderr") or ""
        return clean_text(redact_text(f"exit {exit_code}: {detail}"), MAX_ERROR)
    return None


def _is_bad_end(reason: Optional[str]) -> bool:
    if not reason:
        return False
    lowered = str(reason).lower()
    if lowered in _GOOD_END_REASONS:
        return False
    return any(token in lowered for token in _BAD_END_TOKENS)


def _session_row(row: sqlite3.Row) -> Dict[str, Any]:
    started = datetime.fromtimestamp(float(row["started_at"]), tz=SHANGHAI)
    ended_raw = row["ended_at"]
    ended = datetime.fromtimestamp(float(ended_raw), tz=SHANGHAI) if ended_raw is not None else None
    duration = int(round((ended - started).total_seconds() / 60)) if ended else None
    if duration is not None and duration < 0:
        duration = None
    return {
        "id": str(row["id"]),
        "source": str(row["source"] or ""),
        "started_at": _iso(started),
        "ended_at": _iso(ended) if ended else None,
        "duration_min": duration,
        "end_reason": str(row["end_reason"]) if row["end_reason"] else None,
        "message_count": int(row["message_count"] or 0),
        "tool_call_count": int(row["tool_call_count"] or 0),
        "tokens": {"in": int(row["input_tokens"] or 0), "out": int(row["output_tokens"] or 0)},
        "first_user_msg": "",
        "write_actions": [],
        "read_only_tool_top": [],
        "errors": [],
    }


def _load_cron_jobs(state_dir: Path) -> List[Dict[str, Any]]:
    """读取 <state-dir>/cron/jobs.json；缺失/损坏时返回空列表。"""
    path = state_dir / "cron" / "jobs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def _resolve_exclusions(
    names: Sequence[str],
    job_ids: Sequence[str],
    state_dir: Path,
) -> Tuple[List[str], List[str]]:
    """合并 CLI/环境里的排除目标，并把 job 名解析成 job id。

    cron 在运行预跑脚本时会注入 VIGIL_CRON_JOB_ID / VIGIL_CRON_JOB_NAME；
    因此默认就能排除「当前 job」自己的历史会话。手动运行时用
    --exclude-job / --exclude-job-id 指定。
    """
    resolved_names: List[str] = [str(n).strip() for n in names if str(n).strip()]
    resolved_ids: List[str] = [str(i).strip() for i in job_ids if str(i).strip()]
    env_id = os.environ.get("VIGIL_CRON_JOB_ID", "").strip()
    env_name = os.environ.get("VIGIL_CRON_JOB_NAME", "").strip()
    if env_id:
        resolved_ids.append(env_id)
    if env_name:
        resolved_names.append(env_name)
    if resolved_names:
        wanted = set(resolved_names)
        for job in _load_cron_jobs(state_dir):
            job_name = str(job.get("name") or "").strip()
            job_id = str(job.get("id") or "").strip()
            if job_name and job_id and job_name in wanted:
                resolved_ids.append(job_id)
    return list(dict.fromkeys(resolved_ids)), list(dict.fromkeys(resolved_names))


def _is_excluded_cron_session(
    row: sqlite3.Row,
    job_ids: Sequence[str],
    job_names: Sequence[str],
) -> bool:
    """当前周报 job 自己的会话不计入作业面（否则周报统计周报流水线）。"""
    if str(row["source"] or "").strip().lower() != "cron":
        return False
    session_id = str(row["id"] or "")
    for job_id in job_ids:
        if job_id and session_id.startswith(f"cron_{job_id}_"):
            return True
    title = str(row["title"] or "").strip()
    for job_name in job_names:
        if job_name and title.startswith(job_name):
            return True
    return False


def collect_sessions(
    con: sqlite3.Connection,
    since: datetime,
    until: datetime,
    *,
    exclude_job_ids: Sequence[str] = (),
    exclude_job_names: Sequence[str] = (),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """返回 (sessions facts, unclassified, gaps)。"""
    since_ts, until_ts = since.timestamp(), until.timestamp()
    rows = con.execute(
        """
        SELECT id, source, title, parent_session_id, started_at, ended_at,
               end_reason, message_count, tool_call_count, input_tokens, output_tokens
        FROM sessions
        WHERE started_at >= ? AND started_at < ?
        ORDER BY started_at, id
        """,
        (since_ts, until_ts),
    ).fetchall()
    excluded_ids = {
        str(row["id"]) for row in rows
        if _is_excluded_cron_session(row, exclude_job_ids, exclude_job_names)
    }
    # 周报流水线会派生 subagent 会话（例如「把这份 MD 发布到 BookStack」）。
    # 父会话已排除时，子会话也一并排除，避免自述残留。
    while excluded_ids:
        descendants = {
            str(row["id"]) for row in rows
            if str(row["parent_session_id"] or "") in excluded_ids
        }
        added = descendants - excluded_ids
        if not added:
            break
        excluded_ids |= added
    rows = [row for row in rows if str(row["id"]) not in excluded_ids]

    sessions: List[Dict[str, Any]] = [_session_row(r) for r in rows]
    by_id = {s["id"]: s for s in sessions}
    gaps: List[str] = []

    if not sessions:
        gaps.append("数据缺：本周期无作业记录")
        return sessions, [], gaps

    # 消息按窗口过滤（会话可能跨越 until）。
    placeholders = ",".join("?" for _ in sessions)
    params: List[Any] = [s["id"] for s in sessions] + [since_ts, until_ts]
    message_rows = con.execute(
        f"""
        SELECT session_id, role, content, tool_calls, tool_name, timestamp
        FROM messages
        WHERE session_id IN ({placeholders}) AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp, id
        """,
        params,
    ).fetchall()

    write_groups: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = defaultdict(dict)
    read_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unclassified: List[Dict[str, Any]] = []
    seen_unclassified = set()
    unclassified_truncated = False

    for msg in message_rows:
        sid = str(msg["session_id"])
        session = by_id.get(sid)
        if session is None:
            continue
        role = str(msg["role"] or "")
        content = msg["content"]

        if role == "user":
            if not session["first_user_msg"]:
                text = clean_text(redact_text(str(content or "")), MAX_FIRST_USER_MSG)
                if text:
                    session["first_user_msg"] = text
            continue

        if role == "tool":
            tool_name = str(msg["tool_name"] or "")
            if tool_name and tool_name != "terminal" and tool_name not in NON_TERMINAL_WRITE_TOOLS:
                read_counts[sid][tool_name] += 1
            err = _tool_error(content)
            if err and err not in session["errors"]:
                if len(session["errors"]) < MAX_ERRORS_PER_SESSION:
                    session["errors"].append(err)
            continue

        if role != "assistant":
            continue

        for tool_name, args in _iter_tool_calls(msg["tool_calls"]):
            if tool_name == "terminal":
                command = args.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                actions = _terminal_actions(command)
                write_action = _pick_write_action(actions)
                if write_action:
                    key = ("terminal", write_action)
                    group = write_groups[sid].get(key)
                    if group is None:
                        write_groups[sid][key] = {
                            "tool": "terminal",
                            "action": write_action,
                            "summary": clean_text(redact_text(command), MAX_SUMMARY),
                            "count": 1,
                        }
                    else:
                        group["count"] += 1
                elif "unknown" in actions:
                    summary = clean_text(redact_text(command), MAX_SUMMARY)
                    dedupe = (sid, summary)
                    if summary and dedupe not in seen_unclassified:
                        if len(unclassified) < MAX_UNCLASSIFIED:
                            seen_unclassified.add(dedupe)
                            unclassified.append({"session_id": sid, "tool": "terminal", "summary": summary})
                        else:
                            unclassified_truncated = True
                continue

            if tool_name in NON_TERMINAL_WRITE_TOOLS:
                summary = _summarize_args(tool_name, args)
                key = (tool_name, tool_name)
                group = write_groups[sid].get(key)
                if group is None:
                    write_groups[sid][key] = {
                        "tool": tool_name,
                        "action": tool_name,
                        "summary": summary,
                        "count": 1,
                    }
                else:
                    group["count"] += 1

    for session in sessions:
        sid = session["id"]
        session["write_actions"] = list(write_groups.get(sid, {}).values())
        top = sorted(read_counts.get(sid, {}).items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        session["read_only_tool_top"] = [[name, count] for name, count in top]

    if unclassified_truncated:
        gaps.append(f"数据缺：unclassified 达到上限 {MAX_UNCLASSIFIED} 条，其余未列出")

    return sessions, unclassified, gaps


# ---------------------------------------------------------------------------
# 拓扑快照与 diff
# ---------------------------------------------------------------------------

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    return clean_text(redact_text(text), MAX_FIELD_VALUE)


def _topology_entities(topology: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """把 topology.yaml 里的顶层实体列表摊平成 {kind/name: entity}。

    topology.yaml 的顶层结构是 environments[] / clusters[] / hosts[] / ...，
    统一按「顶层 list，元素是带 name 的 dict」识别，避免写死字段名。
    """
    entities: Dict[str, Dict[str, Any]] = {}
    for kind, value in topology.items():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            env = item.get("env")
            if not env and kind == "environments":
                env = name
            fields: Dict[str, str] = {}
            for key, raw in item.items():
                if str(key).lower() in _TOPO_SKIP_FIELDS:
                    continue
                fields[str(key)] = _stringify(raw)
            entities[f"{kind}/{name}"] = {
                "kind": kind,
                "name": name,
                "env": clean_text(env, 80),
                "fields": fields,
            }
    return entities


def _load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entities"), dict):
        return None
    return data


def _write_snapshot(path: Path, topology_path: Path, entities: Dict[str, Dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "captured_at": _iso(datetime.now(SHANGHAI)),
        "topology_path": str(topology_path),
        "entities": entities,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def collect_topology(topology_path: Path, state_dir: Path) -> Tuple[Dict[str, Any], List[str]]:
    """读取 topology.yaml，与上次快照 diff；返回 (topology facts, gaps)。"""
    snapshot_path = state_dir / "digest" / "topology-snapshot.json"
    gaps: List[str] = []
    facts: Dict[str, Any] = {
        "snapshot_path": str(snapshot_path),
        "snapshot_missing": True,
        "changed": [],
        "added": [],
        "removed": [],
    }

    if not topology_path.exists():
        gaps.append(f"数据缺：topology.yaml 不存在（{topology_path}）")
        return facts, gaps

    try:
        raw = yaml.safe_load(topology_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        gaps.append(f"数据缺：topology.yaml 读取失败（{clean_text(exc, MAX_SUMMARY)}）")
        return facts, gaps
    if not isinstance(raw, dict):
        gaps.append("数据缺：topology.yaml 顶层不是对象")
        return facts, gaps

    current = _topology_entities(raw)
    previous = _load_snapshot(snapshot_path)
    if previous is None:
        facts["snapshot_missing"] = True
        gaps.append("数据缺：无历史拓扑快照（本次已建立基线）")
    else:
        facts["snapshot_missing"] = False
        prev_entities = previous.get("entities") or {}
        changed: List[Dict[str, Any]] = []
        for key, cur in current.items():
            prev = prev_entities.get(key)
            if not isinstance(prev, dict):
                facts["added"].append({
                    "entity": cur["name"],
                    "env": cur["env"],
                    "source": cur["fields"].get("source") or "topology",
                })
                continue
            prev_fields = prev.get("fields") if isinstance(prev.get("fields"), dict) else {}
            cur_fields = cur["fields"]
            for field in sorted(set(prev_fields) | set(cur_fields)):
                old = prev_fields.get(field, "")
                new = cur_fields.get(field, "")
                if old == new:
                    continue
                changed.append({
                    "entity": cur["name"],
                    "env": cur["env"],
                    "field": field,
                    "old": old,
                    "new": new,
                    "source": cur_fields.get("source") or "topology",
                })
        for key, prev in prev_entities.items():
            if key in current or not isinstance(prev, dict):
                continue
            facts["removed"].append({
                "entity": prev.get("name", key),
                "env": prev.get("env", ""),
                "source": (prev.get("fields") or {}).get("source") or "topology",
            })
        facts["changed"] = sorted(changed, key=lambda x: (x["env"], x["entity"], x["field"]))
        facts["added"].sort(key=lambda x: (x["env"], x["entity"]))
        facts["removed"].sort(key=lambda x: (x["env"], x["entity"]))

    try:
        _write_snapshot(snapshot_path, topology_path, current)
    except OSError as exc:
        gaps.append(f"数据缺：拓扑快照写入失败（{clean_text(exc, MAX_SUMMARY)}）")
    return facts, gaps


# ---------------------------------------------------------------------------
# git（只读取数，不改仓库）
# ---------------------------------------------------------------------------

def _run_git(repo: str, args: Sequence[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def collect_repos(repo_paths: Sequence[str], since: datetime, until: datetime) -> Tuple[List[Dict[str, Any]], List[str]]:
    """git log --since/--until --numstat 只读取数；不 fetch、不 clean、不改文件。"""
    repos: List[Dict[str, Any]] = []
    gaps: List[str] = []
    for raw in repo_paths:
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_dir():
            gaps.append(f"数据缺：仓库目录不存在（{path}）")
            repos.append({"path": str(path), "dirty": False, "commits": []})
            continue
        resolved = str(path.resolve())
        try:
            status = _run_git(resolved, ["status", "--porcelain"])
            if status.returncode != 0:
                gaps.append(f"数据缺：不是 git 仓库（{resolved}）")
                repos.append({"path": resolved, "dirty": False, "commits": []})
                continue
            dirty = bool(status.stdout.strip())
            log = _run_git(
                resolved,
                [
                    "log",
                    f"--since={_iso(since)}",
                    f"--until={_iso(until)}",
                    "--date=short",
                    "--pretty=format:%H%x1f%ad%x1f%an%x1f%s",
                    "--numstat",
                ],
            )
            if log.returncode != 0:
                gaps.append(f"数据缺：git log 失败（{resolved}: {clean_text(log.stderr, MAX_SUMMARY)}）")
                repos.append({"path": resolved, "dirty": dirty, "commits": []})
                continue
        except (OSError, subprocess.TimeoutExpired) as exc:
            gaps.append(f"数据缺：git 调用失败（{resolved}: {clean_text(exc, MAX_SUMMARY)}）")
            repos.append({"path": resolved, "dirty": False, "commits": []})
            continue

        commits: List[Dict[str, str]] = []
        for line in log.stdout.splitlines():
            if "\x1f" not in line:
                continue
            parts = line.split("\x1f", 3)
            if len(parts) < 4 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                continue
            sha, day, author, subject = parts
            commits.append({
                "sha": sha[:7],
                "date": clean_text(day, 10),
                "author": clean_text(redact_text(author), 80),
                "subject": clean_text(redact_text(subject), MAX_SUMMARY),
            })
            if len(commits) >= MAX_COMMITS_PER_REPO:
                gaps.append(f"数据缺：仓库 {resolved} 提交数超过 {MAX_COMMITS_PER_REPO}，已截断")
                break
        repos.append({"path": resolved, "dirty": dirty, "commits": commits})
    return repos, gaps


# ---------------------------------------------------------------------------
# facts 构建
# ---------------------------------------------------------------------------

def build_facts(
    *,
    db_path: Path,
    window: Dict[str, str],
    topology_path: Path,
    state_dir: Path,
    repo_paths: Sequence[str],
    exclude_job_ids: Sequence[str] = (),
    exclude_job_names: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    since = datetime.fromisoformat(window["since"])
    until = datetime.fromisoformat(window["until"])

    job_ids, job_names = _resolve_exclusions(
        exclude_job_names, exclude_job_ids, state_dir
    )
    con = _open_db(db_path)
    try:
        sessions, unclassified, session_gaps = collect_sessions(
            con,
            since,
            until,
            exclude_job_ids=job_ids,
            exclude_job_names=job_names,
        )
    finally:
        con.close()

    topology, topology_gaps = collect_topology(topology_path, state_dir)
    repos, repo_gaps = collect_repos(repo_paths, since, until)

    gaps: List[str] = []
    gaps.extend(session_gaps)
    gaps.extend(topology_gaps)
    gaps.extend(repo_gaps)
    if not repo_paths:
        gaps.append("数据缺：未指定 --repos，未采集代码提交")

    total_messages = sum(s["message_count"] for s in sessions)
    total_writes = sum(a["count"] for s in sessions for a in s["write_actions"])
    failed_sessions = sum(
        1 for s in sessions if s["errors"] or _is_bad_end(s.get("end_reason"))
    )
    tokens_in = sum(s["tokens"]["in"] for s in sessions)
    tokens_out = sum(s["tokens"]["out"] for s in sessions)

    return {
        "window": window,
        "generated_at": _iso(now),
        "db": str(db_path.resolve()),
        "totals": {
            "sessions": len(sessions),
            "messages": total_messages,
            "write_actions": total_writes,
            "failed_sessions": failed_sessions,
            "tokens": {"in": tokens_in, "out": tokens_out},
        },
        "sessions": sessions,
        "unclassified": unclassified,
        "topology": topology,
        "repos": repos,
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# Markdown 渲染（D3 契约；数据只能来自 facts）
# ---------------------------------------------------------------------------

def _short_time(iso_text: Optional[str]) -> str:
    if not iso_text:
        return "-"
    try:
        return datetime.fromisoformat(iso_text).strftime("%m-%d %H:%M")
    except ValueError:
        return clean_text(iso_text, 16)


def _end_reason_text(reason: Optional[str]) -> str:
    mapping = {
        "turn_complete": "完成",
        "cli_close": "正常关闭",
        "tui_close": "正常关闭",
        "done": "完成",
        "completed": "完成",
        "complete": "完成",
        "interrupted": "中断",
        "approval_timeout": "审批超时",
        "timeout": "超时",
        "error": "失败",
    }
    return mapping.get(str(reason or ""), clean_text(reason, 40) or "未知")


def _session_main_action(session: Dict[str, Any]) -> str:
    actions = session.get("write_actions") or []
    if actions:
        parts = [f"{a['action']}×{a['count']}" for a in actions[:3]]
        if len(actions) > 3:
            parts.append("…")
        return "、".join(parts)
    top = session.get("read_only_tool_top") or []
    if top:
        return "、".join(f"{name}×{count}" for name, count in top[:3])
    if int(session.get("tool_call_count") or 0) > 0:
        return f"工具调用 {int(session['tool_call_count'])} 次"
    return "无工具调用"


def _summary_bullets(facts: Dict[str, Any]) -> List[str]:
    totals = facts.get("totals") or {}
    topology = facts.get("topology") or {}
    bullets = [
        f"本周期 {int(totals.get('sessions', 0))} 个会话，"
        f"{int(totals.get('messages', 0))} 条消息，"
        f"{int(totals.get('write_actions', 0))} 次写操作。",
    ]
    topo_count = (
        len(topology.get("changed") or [])
        + len(topology.get("added") or [])
        + len(topology.get("removed") or [])
    )
    gaps_text = " ".join(facts.get("gaps") or [])
    if topo_count:
        bullets.append(f"拓扑变更 {topo_count} 条。")
    elif "topology.yaml 不存在" in gaps_text:
        bullets.append("拓扑数据缺，未判定变更。")
    else:
        bullets.append("本周期无拓扑变更。")
    failed = int(totals.get("failed_sessions", 0))
    bullets.append(f"异常或未闭环会话 {failed} 个。" if failed else "无异常或未闭环会话。")
    repos = facts.get("repos") or []
    if repos:
        commits = sum(len(r.get("commits") or []) for r in repos)
        bullets.append(f"代码仓库 {len(repos)} 个，提交 {commits} 个。")
    else:
        bullets.append("未采集代码仓库。")
    gaps = facts.get("gaps") or []
    if gaps:
        bullets.append(f"数据缺口 {len(gaps)} 条，见第 4 段。")
    # 摘要 ≤5 条、每条 ≤40 字。
    return [bullet[:40] for bullet in bullets[:5]]


def render_markdown(facts: Dict[str, Any]) -> str:
    """按 D3 固定 5 段渲染；所有表格数据只来自 facts。"""
    window = facts.get("window") or {}
    label = clean_text(window.get("label"), 20) or "未知周"
    since_text = window.get("since")
    until_text = window.get("until")
    try:
        since_dt = datetime.fromisoformat(since_text)
        until_dt = datetime.fromisoformat(until_text)
        span = f"{since_dt:%m-%d} ~ {until_dt:%m-%d}"
    except (TypeError, ValueError):
        span = "日期缺"

    lines: List[str] = [f"# Vigil 周报 {label}（{span}）", ""]

    # 1. 摘要
    lines.append("## 1. 摘要")
    for bullet in _summary_bullets(facts):
        lines.append(f"- {strip_emoji(bullet)}")
    lines.append("")

    # 2. 变更明细
    lines.append("## 2. 变更明细")
    topology = facts.get("topology") or {}
    rows: List[Tuple[str, str, str, str]] = []
    for item in topology.get("changed") or []:
        change = f"{item.get('field', '')}: {item.get('old', '')} -> {item.get('new', '')}"
        rows.append((item.get("env", ""), item.get("entity", ""), change, item.get("source", "")))
    for item in topology.get("added") or []:
        rows.append((item.get("env", ""), item.get("entity", ""), "新增", item.get("source", "")))
    for item in topology.get("removed") or []:
        rows.append((item.get("env", ""), item.get("entity", ""), "移除", item.get("source", "")))
    if rows:
        lines.append("| 环境 | 实体 | 变化 | 来源 |")
        lines.append("|---|---|---|---|")
        for env, entity, change, source in rows:
            lines.append(f"| {md_cell(env) or '-'} | {md_cell(entity)} | {md_cell(change)} | {md_cell(source)} |")
    else:
        gaps_text = " ".join(facts.get("gaps") or [])
        if "topology.yaml 不存在" in gaps_text:
            note = "本周期无拓扑变更（topology.yaml 缺失，变更面数据缺）。"
        elif topology.get("snapshot_missing"):
            note = "本周期无拓扑变更。（首次采集，无历史快照，本次已建立基线。）"
        else:
            note = "本周期无拓扑变更。"
        lines.append(note)
    lines.append("")

    # 3. 作业明细
    lines.append("## 3. 作业明细")
    sessions = facts.get("sessions") or []
    if sessions:
        lines.append("| 时间 | 会话 | 主要动作 | 写操作 | 结果 |")
        lines.append("|---|---|---|---|---|")
        for session in sessions:
            writes = sum(a.get("count", 0) for a in session.get("write_actions") or [])
            result = _end_reason_text(session.get("end_reason"))
            errors = session.get("errors") or []
            if errors:
                result += f"（{len(errors)} 条错误）"
            lines.append(
                "| {time} | {sid} | {action} | {writes} | {result} |".format(
                    time=md_cell(_short_time(session.get("started_at"))),
                    sid=md_cell(session.get("id")),
                    action=md_cell(_session_main_action(session)),
                    writes=writes,
                    result=md_cell(result),
                )
            )
    else:
        lines.append("本周期无作业记录。")
    lines.append("")

    # 4. 风险与未闭环
    lines.append("## 4. 风险与未闭环")
    risk_lines: List[str] = []
    for gap in facts.get("gaps") or []:
        risk_lines.append(f"- {strip_emoji(gap)}")
    for session in sessions:
        errors = session.get("errors") or []
        bad_end = _is_bad_end(session.get("end_reason"))
        if not errors and not bad_end:
            continue
        reason = _end_reason_text(session.get("end_reason"))
        suffix = f"，{len(errors)} 条错误" if errors else ""
        if bad_end:
            risk_lines.append(f"- 未闭环：会话 {session.get('id')}（{reason}{suffix}）")
        else:
            risk_lines.append(f"- 工具错误：会话 {session.get('id')}（已结束：{reason}，{len(errors)} 条）")
        for err in errors:
            risk_lines.append(f"- 错误：{session.get('id')}：{strip_emoji(err)}")
    if risk_lines:
        omitted = max(0, len(risk_lines) - MAX_MD_RISK_LINES)
        lines.extend(risk_lines[:MAX_MD_RISK_LINES])
        if omitted:
            lines.append(f"- 其余 {omitted} 条风险项已省略。")
    else:
        lines.append("无。")
    lines.append("")

    # 5. 下周期（1-3 条，必须对应第 4 段）
    lines.append("## 5. 下周期")
    next_items: List[str] = []
    for gap in (facts.get("gaps") or [])[:1]:
        next_items.append(f"补齐数据：{strip_emoji(gap).removeprefix('数据缺：')}")
    for session in sessions:
        if len(next_items) >= 3:
            break
        if session.get("errors") or _is_bad_end(session.get("end_reason")):
            if _is_bad_end(session.get("end_reason")):
                next_items.append(f"跟进未闭环会话 {session.get('id')}（{_end_reason_text(session.get('end_reason'))}）。")
            else:
                next_items.append(f"复查会话 {session.get('id')} 的工具错误。")
    if not next_items:
        next_items.append("本周期无未闭环项，按计划推进。")
    for index, item in enumerate(next_items[:3], start=1):
        lines.append(f"{index}. {strip_emoji(item)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Vigil 周期变更汇总采集器（facts / Markdown，只读取数）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "窗口左闭右开 [since, until)，时区 Asia/Shanghai。\n"
            "示例：python cron/scripts/weekly_change_digest.py --db ~/.vigil/state.db --window 7d --format facts"
        ),
    )
    parser.add_argument("--db", default=None, help="state.db 路径（默认 <VIGIL_HOME>/state.db）")
    parser.add_argument("--window", default=None, help="滚动窗口，形如 7d / 24h / 2w（默认 7d）")
    parser.add_argument("--week", default=None, help="ISO 周，形如 2026-W44")
    parser.add_argument("--since", default=None, help="窗口起点（含），YYYY-MM-DD 或 ISO 8601")
    parser.add_argument("--until", default=None, help="窗口终点（不含），YYYY-MM-DD 或 ISO 8601")
    parser.add_argument("--topology", default=None, help="topology.yaml 路径（默认 <state-dir>/topology.yaml）")
    parser.add_argument("--state-dir", default=None, help="数据根（默认 --db 所在目录）")
    parser.add_argument("--repos", default=None, help="逗号分隔的 git 仓库路径（只读取数）")
    parser.add_argument(
        "--exclude-job",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "排除该 cron job 的会话（可重复）。默认读取 VIGIL_CRON_JOB_NAME，"
            "并按 <state-dir>/cron/jobs.json 把名字解析成 job id。"
        ),
    )
    parser.add_argument(
        "--exclude-job-id",
        action="append",
        default=None,
        metavar="ID",
        help="排除该 cron job id 的会话（可重复）。默认读取 VIGIL_CRON_JOB_ID。",
    )
    parser.add_argument("--format", choices=("facts", "md"), default="facts", help="输出格式（默认 facts）")
    parser.add_argument("--out", default=None, help="输出文件路径（默认 stdout）")
    return parser


def _write_out(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        window = resolve_window(args)
    except ValueError as exc:
        parser.error(str(exc))  # argparse 以退出码 2 结束

    db_path = Path(args.db).expanduser() if args.db else _home_dir() / "state.db"
    if not db_path.exists():
        print(f"weekly_change_digest: 数据库不存在: {db_path}", file=sys.stderr)
        return 3
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else db_path.resolve().parent
    topology_path = Path(args.topology).expanduser() if args.topology else state_dir / "topology.yaml"
    repo_paths = [p for p in (args.repos or "").split(",") if p.strip()]

    try:
        facts = build_facts(
            db_path=db_path,
            window=window,
            topology_path=topology_path,
            state_dir=state_dir,
            repo_paths=repo_paths,
            exclude_job_ids=args.exclude_job_id or (),
            exclude_job_names=args.exclude_job or (),
        )
    except (sqlite3.Error, OSError) as exc:
        print(f"weekly_change_digest: 数据库打开失败: {exc}", file=sys.stderr)
        return 3

    if args.format == "facts":
        text = json.dumps(facts, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_markdown(facts)

    if args.out:
        out_path = Path(args.out).expanduser()
        try:
            _write_out(out_path, text)
        except OSError as exc:
            print(f"weekly_change_digest: 写出失败: {exc}", file=sys.stderr)
            return 2
        print(f"weekly_change_digest: 已写出 {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
