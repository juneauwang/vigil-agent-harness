#!/usr/bin/env python3
"""Vigil 作业日志（digest）：按「我提的任务 → 实际做的变更」汇总一个时间窗。

原 ``cron/scripts/oplog_digest.py`` 的采集/渲染逻辑搬到这里，作为随 wheel 分发的
内置能力；旧脚本保留为薄 shim。facts 字段名与抽取规则不变。

用法：
    vigil digest --window 7d --format facts
    vigil digest --window 2026-W43 --format md --out /tmp/r.md
    vigil digest --window 7d --format md --publish bookstack
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SHANGHAI = timezone(timedelta(hours=8))
CRON_HDR = re.compile(r"^\[IMPORTANT: You are running as a scheduled cron job")
MAX_ASK = 140
MAX_DETAIL = 160
MAX_CHANGES_PER_TASK = 40
EXCLUDED_SOURCES = ("cron", "subagent")

REPORT_PREFIX = "Vigil-作业周报-"

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?\S+"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[=:]\s*\S+"),
]


class DigestPublishError(Exception):
    """发布失败（消息里不得含凭据）。"""


def redact(text: str) -> str:
    value = str(text or "")
    for pat in _SECRET_PATTERNS:
        value = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", value)
    return value


def clean(text: object, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return redact(value)[:limit]


def ask_line(text: str) -> str:
    """用户意图取第一句（避免把整段交接报告当成任务）。"""
    head = re.split(r"[\n。?!?!；;]", str(text or "").strip(), maxsplit=1)[0]
    return clean(head, 100)


# --------------------------------------------------------------------------
# 时间窗
# --------------------------------------------------------------------------

def parse_window(args, now: datetime) -> Tuple[datetime, datetime, str]:
    if args.week and (args.window or args.since or args.until):
        raise ValueError("--week 不能与 --window/--since/--until 同时使用")
    if args.since and args.window:
        raise ValueError("--since/--until 不能与 --window 同时使用")
    if args.week:
        m = re.fullmatch(r"(\d{4})-W(\d{1,2})", args.week.strip())
        if not m:
            raise ValueError("--week 需要形如 2026-W43")
        year, week = int(m.group(1)), int(m.group(2))
        try:
            monday = datetime.combine(
                date.fromisocalendar(year, week, 1), datetime.min.time(), SHANGHAI
            )
        except ValueError as exc:
            raise ValueError(f"非法 ISO 周：{args.week!r}") from exc
        return monday, monday + timedelta(days=7), f"{year}-W{week:02d}"
    if args.since:
        if not args.until:
            raise ValueError("--since 与 --until 必须同时提供")
        since = _parse_dt(args.since)
        until = _parse_dt(args.until)
        if since >= until:
            raise ValueError("--since 必须早于 --until")
        return since, until, _label(until - timedelta(seconds=1))
    m = re.fullmatch(r"(\d+)([dhmw])", (args.window or "7d").strip(), re.IGNORECASE)
    if not m:
        raise ValueError("--window 需要形如 7d / 24h / 2w")
    n, unit = int(m.group(1)), m.group(2).lower()
    if n <= 0:
        raise ValueError("--window 必须大于 0")
    delta = {
        "d": timedelta(days=n),
        "h": timedelta(hours=n),
        "m": timedelta(minutes=n),
        "w": timedelta(weeks=n),
    }[unit]
    since = now - delta
    return since, now, _label(now - timedelta(seconds=1))


def _parse_dt(text: str) -> datetime:
    text = str(text).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = datetime.combine(date.fromisoformat(text), datetime.min.time(), SHANGHAI)
        return parsed
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _label(dt: datetime) -> str:
    iso = dt.astimezone(SHANGHAI).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# --------------------------------------------------------------------------
# 变更项抽取
# --------------------------------------------------------------------------

def _json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _fmt_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return clean(v, 48)


def change_from_call(name: str, raw) -> Optional[dict]:
    """把一次写类工具调用折成一条变更记录。"""
    a = _json(raw)
    if name == "topo_update":
        updates = a.get("updates") or {}
        detail = "; ".join(f"{k}={_fmt_value(v)}" for k, v in updates.items()) or "（无字段）"
        return {
            "kind": "拓扑",
            "target": a.get("entity") or "?",
            "detail": clean(detail, MAX_DETAIL),
            "why": clean(a.get("reason"), 100),
        }
    if name in ("write_file", "patch"):
        path = a.get("path") or a.get("file_path") or "?"
        action = "改" if name == "patch" else "写"
        head = a.get("new_string") if name == "patch" else a.get("content")
        return {
            "kind": "文件",
            "target": path,
            "detail": f"{action}：{purpose_of(head, path)}",
            "why": "",
        }
    if name == "runbook_execute":
        ctx = a.get("trigger_context") or {}
        return {
            "kind": "runbook",
            "target": f"{a.get('runbook') or '?'}",
            "detail": f"env={a.get('env') or '-'}",
            "why": clean(ctx.get("reason"), 100),
        }
    if name == "skill_manage":
        target = a.get("name") or "?"
        detail = " ".join(x for x in (a.get("action"), a.get("file_path")) if x) or "patch"
        return {"kind": "技能", "target": target, "detail": detail, "why": ""}
    if name == "memory":
        ops = a.get("operations") or []
        heads = [f"{o.get('action')}:{clean(o.get('content'), 40)}" for o in ops[:2] if isinstance(o, dict)]
        return {"kind": "记忆", "target": a.get("target") or "-", "detail": "; ".join(heads) or "update", "why": ""}
    if name == "publish_bookstack":
        return {"kind": "发布", "target": a.get("name") or "?", "detail": "BookStack 页面", "why": ""}
    return None


# 段首可剥的前缀（命令链被拆段后常见）
_LEAD_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:(?:sudo|doas|nohup|time|exec|command|do|then|else|if)\s+)+"
)
# 段首的变量赋值（`IMG=xxx kubectl ...`、`BK=$(date) cp ...`）
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# 必须在段首（剥掉前缀后）才算数的动作——避免 grep 'scp|ssh' 这类文本误命中
_LEADING_WRITE_RE = re.compile(
    r"(?i)^(?P<lead>"
    r"kubectl\s+(?P<kubectl>apply|delete|create|edit|scale|rollout|patch|drain|cordon|taint|label|annotate)"
    r"|helm\s+(?P<helm>install|upgrade|uninstall|rollback)"
    r"|systemctl\s+(?P<systemctl>start|stop|restart|reload|enable|disable|mask|unmask)"
    r"|docker(?:-compose|\s+compose)\s+(?P<compose>up|down|restart|start|stop|rm)"
    r"|docker\s+(?P<docker>run|rm|stop|start|restart|build|push|exec)"
    r"|(?P<scp>scp|rsync)"
    r"|(?P<fileop>rm|mv|cp|chmod|chown|mkfs|truncate|dd)"
    r"|git\s+(?P<git>push|reset|checkout|commit|merge|rebase)"
    r"|(?P<svc>reboot|shutdown)"
    r")(?=\s|$)"
)

_READ_ONLY_HEADS = (
    "echo", "printf", "ls", "cat", "head", "tail", "grep", "egrep", "rg", "awk", "jq",
    "sed", "test", "which", "whoami", "id", "date", "uptime", "df", "du", "free",
    "ss", "netstat", "ps", "env", "printenv", "curl", "wget", "ping", "dig", "nslookup",
)


def _strip_prefixes(text: str) -> str:
    """剥掉段首的 sudo/do/nohup 之类前缀与变量赋值（可能叠多层）。"""
    out = text
    for _ in range(4):
        new = _LEAD_PREFIX_RE.sub("", out)
        new = _ASSIGN_RE.sub("", new, count=1)
        if new == out:
            break
        out = new
    return out.strip()


def _verb_from_leading(text: str) -> Optional[str]:
    m = _LEADING_WRITE_RE.match(_strip_prefixes(text))
    if not m:
        return None
    for group, value in m.groupdict().items():
        if group != "lead" and value:
            return f"{group}:{value.lower()}"
    return None


def write_verb(command: str) -> Optional[str]:
    """从命令里认出「写类动作」并归一成短标签；认不出返回 None（只读/探测）。

    只在**段首**（剥掉 sudo/赋值前缀后）认，避免 `grep -nE 'scp|ssh'` 这类文本
    把只读命令误判成写操作；`ssh host 'kubectl apply …'` 这种远程包装再对引号
    内的内容做一次段首匹配。
    """
    text = str(command or "")
    if not text.strip():
        return None
    verb = _verb_from_leading(text)
    if verb:
        return verb
    # 引号内的远程命令（`ssh host 'kubectl apply …'`）再试一次段首匹配；
    # 但 grep/awk/sed 的模式串不是命令，跳过，否则 `grep 'scp |ssh'` 会误判。
    head = _strip_prefixes(text).split(None, 1)[0].lstrip("./") if text.strip() else ""
    if head in {"grep", "egrep", "rg", "awk", "sed", "find", "ls", "cat"}:
        return None
    for quoted in _QUOTED_RE.findall(text):
        inner = quoted[1:-1].strip()
        if not inner:
            continue
        for part in _SEG_SPLIT_RE.split(inner):
            verb = _verb_from_leading(part)
            if verb:
                return verb
    return None


def looks_read_only(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return True
    if write_verb(text):
        return False
    # 含 -X POST/PUT/PATCH/DELETE 或 --data 的 curl 也算写。
    # 数据类 flag 区分大小写——`-d` 是 data，`-D` 是 dump-header（只读探测）。
    if re.search(r"(?i)\bcurl\b", text) and re.search(
        r"(?i:-X\s*(?:POST|PUT|PATCH|DELETE))|--data\b|(?<![A-Za-z])-d\s|--upload-file|(?<![A-Za-z])-F\s",
        text,
    ):
        return False
    return True


_SEG_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")


def _segments(command: str) -> list[str]:
    """按链式操作符拆段（管道不拆：`curl ... | head` 仍算一条）。"""
    return [seg.strip() for seg in _SEG_SPLIT_RE.split(str(command or "")) if seg.strip()]


def _segment_is_upload(seg: str) -> bool:
    """该段是不是「带数据体的 HTTP 请求」（curl/wget 上传）。

    只看管道的第一节：`curl … | tr -d '\\n'` 里的 `-d` 属于 tr，不算上传。
    """
    head = seg.split("|", 1)[0]
    if not re.search(r"(?i)\b(curl|wget)\b", head):
        return False
    return bool(
        re.search(
            r"(?i:-X\s*(?:POST|PUT|PATCH|DELETE))|--data\b|(?<![A-Za-z])-d\s|--upload-file|(?<![A-Za-z])-F\s",
            head,
        )
    )


def classify_terminal(command: str) -> dict | None:
    """写类终端命令 → 一条变更记录；只读/探测命令返回 None。

    按链式段逐段判定：只有「真正会改东西的那一段」算变更，detail 也只取那一段，
    避免把 `sleep 20; echo xxx` 这种复合命令整条记成写操作。
    """
    for seg in _segments(command):
        verb = write_verb(seg)
        if verb:
            return {"kind": "命令", "target": verb, "detail": clean(seg, 110), "why": ""}
        if _segment_is_upload(seg):
            return {"kind": "命令", "target": "http:upload", "detail": clean(seg, 110), "why": ""}
    return None


def purpose_of(text: str, path: str = "") -> str:
    """从写入内容里取一句「这个文件是干什么的」：优先注释行，其次首个有效行。"""
    body = str(text or "")
    if not body.strip():
        return f"空文件 {Path(path).name}" if path else "空内容"
    lines = [ln.strip() for ln in body.splitlines()]
    comment = None
    first_code = None
    for line in lines[:40]:
        if not line or line.startswith("#!"):
            continue
        if re.match(r"^(#|//|/\*|\*|--)+\s*\S", line):
            cleaned = re.sub(r"^(#|//|/\*|\*|--)+\s*", "", line).strip()
            if cleaned and not cleaned.startswith("-*-"):
                comment = cleaned
                break
        elif first_code is None:
            first_code = line
    # YAML/k8s：优先 kind/name
    if not comment:
        for line in lines[:40]:
            if re.match(r"^(kind|name|metadata|apiVersion):", line):
                first_code = first_code or line
                break
    return clean(comment or first_code or "", 80)


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------

def collect(
    db: Path,
    since: datetime,
    until: datetime,
    extra_job_ids: Optional[Sequence[str]] = None,
    *,
    job_id: Optional[str] = None,
    job_name: Optional[str] = None,
) -> dict:
    """采集一个窗口的 facts。

    ``job_id``/``job_name`` 由内置 cron 运行器直接注入；None 时回退到
    ``VIGIL_CRON_JOB_ID``（兼容预跑脚本路径）。
    """
    del job_name  # 保留参数供内置运行器传递；排除口径只认 job id
    if not db.is_file():
        raise FileNotFoundError(f"state.db 不存在：{db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lo, hi = since.timestamp(), until.timestamp()

    # 自身排除：
    # - 有 job id：只排除本 job 的 cron 会话及其派生 subagent（别把别的定时作业
    #   干的真活抹掉）。
    # - 无 job id（手动跑）：排除全部 cron/subagent 会话。
    effective_job_id = str(
        job_id if job_id is not None else os.environ.get("VIGIL_CRON_JOB_ID") or ""
    ).strip()
    extra_excluded = {str(x).strip() for x in (extra_job_ids or []) if str(x).strip()}

    sessions = conn.execute(
        "select id, source, parent_session_id, started_at, ended_at, end_reason, message_count,"
        " tool_call_count, input_tokens, output_tokens from sessions"
        " where started_at >= ? and started_at < ? order by started_at", (lo, hi)).fetchall()

    prefixes = ([f"cron_{effective_job_id}_"] if effective_job_id else []) + [
        f"cron_{jid}_" for jid in extra_excluded
    ]
    own_ids = {
        row["id"] for row in sessions
        if prefixes and row["source"] == "cron" and any(str(row["id"]).startswith(p) for p in prefixes)
    }
    # 级联：当前 job 会话派生的 subagent 也算自身。
    changed = True
    while changed and own_ids:
        changed = False
        for row in sessions:
            if row["id"] in own_ids:
                continue
            if row["parent_session_id"] in own_ids:
                own_ids.add(row["id"])
                changed = True

    tasks, skipped = [], 0
    for row in sessions:
        if row["id"] in own_ids:
            skipped += 1
            continue
        if not effective_job_id and row["source"] in EXCLUDED_SOURCES:
            skipped += 1
            continue

        asks = []
        for m in conn.execute("select content from messages where session_id=? and role='user'"
                              " order by id", (row["id"],)):
            text = str(m["content"] or "")
            if not text.strip() or CRON_HDR.match(text):
                continue
            asks.append(ask_line(text))
            if len(asks) >= 3:
                break

        changes, seen = [], set()
        for m in conn.execute("select tool_calls from messages where session_id=? and tool_calls is not null"
                              " order by id", (row["id"],)):
            for call in _json_list(m["tool_calls"]):
                fn = (call or {}).get("function") or {}
                name, raw = fn.get("name"), fn.get("arguments")
                item = None
                if name == "terminal":
                    args = _json(raw)
                    item = classify_terminal(str(args.get("command") or ""))
                else:
                    item = change_from_call(str(name or ""), raw)
                if not item:
                    continue
                key = (item["kind"], item["target"])
                if key in seen:
                    for existing in changes:
                        if (existing["kind"], existing["target"]) == key and item.get("why"):
                            existing["why"] = item["why"]
                    continue
                seen.add(key)
                changes.append(item)

        if not changes and not asks:
            continue
        tasks.append({
            "when": datetime.fromtimestamp(row["started_at"], SHANGHAI).strftime("%m-%d %H:%M"),
            "session": row["id"],
            "source": row["source"],
            "asks": asks,
            "changes": changes[:MAX_CHANGES_PER_TASK],
            "change_total": len(changes),
            "end_reason": row["end_reason"],
        })

    by_kind: Dict[str, int] = {}
    for task in tasks:
        for ch in task["changes"]:
            by_kind[ch["kind"]] = by_kind.get(ch["kind"], 0) + 1

    days: List[dict] = []
    for index, task in enumerate(tasks):
        day = task["when"][:5]
        bucket = next((d for d in days if d["date"] == day), None)
        if bucket is None:
            bucket = {"date": day, "task_indexes": [], "changes": 0, "kinds": {}}
            days.append(bucket)
        bucket["task_indexes"].append(index)
        bucket["changes"] += task["change_total"]
        for ch in task["changes"]:
            bucket["kinds"][ch["kind"]] = bucket["kinds"].get(ch["kind"], 0) + 1

    return {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "label": _label(until - timedelta(seconds=1)),
        },
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "db": str(db),
        "totals": {
            "tasks": len(tasks),
            "changes": sum(t["change_total"] for t in tasks),
            "by_kind": by_kind,
            "excluded_pipeline_sessions": skipped,
            "active_days": len(days),
        },
        "days": days,
        "tasks": tasks,
        "gaps": [] if tasks else ["本周期没有用户会话（只读库或窗口内无作业）"],
    }


def _json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    return value if isinstance(value, list) else []


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def render_md(facts: dict, prose: Optional[Dict[str, str]] = None) -> str:
    """渲染五段报告。``prose`` 给了就用模型写的叙述（第 1/4/5 段），否则用确定性模板。

    表格与数字永远来自 facts，prose 只能替换叙述段落——这样模型不进不可校验主路径。
    """
    prose = prose or {}
    w = facts["window"]
    t = facts["totals"]
    since = datetime.fromisoformat(w["since"]).strftime("%m-%d")
    until = (datetime.fromisoformat(w["until"]) - timedelta(seconds=1)).strftime("%m-%d")
    lines = [f"# Vigil 作业周报 {w['label']}（{since} ~ {until}）", ""]
    lines.append(f"- 人工作业会话 {t['tasks']} 个 / 变更 {t['changes']} 项")
    kinds = "、".join(f"{k} {v}" for k, v in sorted(t["by_kind"].items())) or "无"
    lines.append(f"- 变更构成：{kinds}")
    lines.append(f"- 已排除流水线自身会话：{t['excluded_pipeline_sessions']} 个")
    lines += ["", "## 1. 本周做了什么", ""]
    summary = str(prose.get("summary") or "").strip()
    if summary:
        lines.extend(summary.splitlines())
        lines.append("")
    elif not facts["tasks"]:
        lines.append("本周期无作业记录。")
    else:
        for day in facts.get("days") or []:
            lines.append(f"### {day['date']}（{len(day['task_indexes'])} 个任务 / {day['changes']} 项变更）")
            lines.append("")
            for idx in day["task_indexes"]:
                task = facts["tasks"][idx]
                ask = task["asks"][0] if task["asks"] else "（无用户指令记录）"
                lines.append(f"- {ask} → 结束状态 {task['end_reason'] or '-'}，变更 {task['change_total']} 项")
            lines.append("")

    structured = ("拓扑", "文件", "runbook", "技能", "发布", "记忆")
    lines += ["", "## 2. 变更明细（服务 / 配置 / 参数）", "",
              "| 时间 | 类型 | 对象 | 变更内容 | 会话 |", "|---|---|---|---|---|"]
    rows = 0
    for task in facts["tasks"]:
        for ch in task["changes"]:
            if ch["kind"] not in structured:
                continue
            rows += 1
            lines.append(f"| {task['when']} | {ch['kind']} | {ch['target']} | {ch['detail']} | {task['session'][:12]} |")
    if rows == 0:
        lines.append("| - | - | - | 本周期无结构化变更 | - |")

    lines += ["", "## 3. 执行过的写类命令（摘）", ""]
    cmds = [(task, ch) for task in facts["tasks"] for ch in task["changes"] if ch["kind"] == "命令"]
    if not cmds:
        lines.append("无。")
    for task, ch in cmds[:12]:
        lines.append(f"- {task['when']} [{ch['target']}] `{ch['detail'][:100]}`")
    if len(cmds) > 12:
        lines.append(f"- 另有 {len(cmds) - 12} 条同类命令未列出")

    lines += ["", "## 4. 未闭环与待跟进", ""]
    risks = str(prose.get("risks") or "").strip()
    pending = [task for task in facts["tasks"] if (task["end_reason"] or "") not in ("cli_close", "turn_complete")]
    if risks:
        lines.extend(risks.splitlines())
        lines.append("")
    elif not pending and not facts["gaps"]:
        lines.append("无。")
    for task in pending:
        lines.append(f"- {task['when']} {task['asks'][0] if task['asks'] else task['session']}：结束状态 {task['end_reason'] or '未知'}")
    for gap in facts["gaps"]:
        lines.append(f"- 数据缺：{gap}")
    next_steps = str(prose.get("next") or "").strip()
    lines += ["", "## 5. 下周期", ""]
    lines.extend(next_steps.splitlines() if next_steps else ["- 待补：由撰写者根据第 4 段给出 1-3 条。"])
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 发布（配置 + credential_vault + BookStack）
# --------------------------------------------------------------------------

def _load_publish_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    publish = ((cfg.get("ops") or {}).get("digest") or {}).get("publish") or {}
    return publish if isinstance(publish, dict) else {}


def default_publish_target() -> str:
    """config.yaml 里显式设置的发布目标（未配置/非法返回空串）。"""
    target = str(_load_publish_config().get("target") or "").strip().lower()
    return target if target in ("none", "file", "bookstack") else ""


def publish_configured() -> bool:
    """config.yaml 里是否配置了可用的 BookStack 发布目标。"""
    publish = _load_publish_config()
    if str(publish.get("target") or "").strip().lower() != "bookstack":
        return False
    bookstack = publish.get("bookstack") or {}
    return bool(
        isinstance(bookstack, dict)
        and str(bookstack.get("base_url") or "").strip()
        and bookstack.get("book_id")
        and str(bookstack.get("credential_ref") or "").strip()
    )


def _bookstack_credentials() -> Tuple[str, int, str, str]:
    publish = _load_publish_config()
    bookstack = publish.get("bookstack") or {}
    if not isinstance(bookstack, dict) or str(publish.get("target") or "").lower() != "bookstack":
        raise DigestPublishError(
            "未配置 BookStack 发布目标：请在 config.yaml 的 "
            "ops.digest.publish.bookstack 下设置 base_url / book_id / credential_ref。"
        )
    base_url = str(bookstack.get("base_url") or "").strip()
    book_id_raw = bookstack.get("book_id")
    ref = str(bookstack.get("credential_ref") or "").strip()
    missing = [
        key for key, value in (
            ("base_url", base_url), ("book_id", book_id_raw), ("credential_ref", ref),
        ) if not value
    ]
    if missing:
        raise DigestPublishError(
            f"BookStack 发布配置缺少 {', '.join(missing)}（ops.digest.publish.bookstack）。"
        )
    try:
        book_id = int(book_id_raw)
    except (TypeError, ValueError) as exc:
        raise DigestPublishError("ops.digest.publish.bookstack.book_id 必须是整数") from exc

    from tools.credential_vault import retrieve
    try:
        raw = retrieve(ref)
    except Exception as exc:
        raise DigestPublishError(
            f"无法读取凭据 {ref!r}：{exc}；先运行 `vigil credential store {ref}` 写入"
            "（值为 token_id:token_secret）。"
        ) from exc

    raw = str(raw or "").strip()
    token_id = secret = ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise DigestPublishError(
                f"凭据 {ref!r} 不是合法 JSON；应为 token_id:token_secret 或 JSON 对象。"
            ) from exc
        token_id = str(data.get("token_id") or "")
        secret = str(data.get("token_secret") or data.get("secret") or "")
    else:
        token_id, sep, secret = raw.partition(":")
        if not sep:
            raise DigestPublishError(
                f"凭据 {ref!r} 格式不对：应为 token_id:token_secret。"
            )
    if not token_id or not secret:
        raise DigestPublishError(
            f"凭据 {ref!r} 缺少 token_id 或 token_secret；用 `vigil credential store {ref}` 更新。"
        )
    return base_url, book_id, token_id, secret


def publish_markdown(
    markdown: str,
    *,
    name: str,
    state_dir: Path,
    target: str,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """把渲染好的 Markdown 发布到 file/bookstack；返回 action/page_id/url 等。"""
    normalized = str(target or "none").strip().lower()
    if normalized == "none":
        return {"action": "none", "target": "none"}
    if normalized == "file":
        path = report_path or (state_dir / "reports" / f"{name}.md")
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return {
            "action": "updated" if existed else "created",
            "target": "file",
            "path": str(path),
        }
    if normalized == "bookstack":
        from tools.publish_targets import BookstackTarget, PublishTargetError

        base_url, book_id, token_id, secret = _bookstack_credentials()
        try:
            publisher = BookstackTarget(base_url, token_id, secret, timeout=30)
            return publisher.publish(name=name, markdown=markdown, book_id=book_id)
        except PublishTargetError as exc:
            raise DigestPublishError(str(exc)) from exc
    raise DigestPublishError(f"未知发布目标：{target!r}（支持 none/file/bookstack）")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--window", default=None, help="滚动窗口，如 7d / 24h / 2w（默认 7d）")
    parser.add_argument("--week", default=None, help="ISO 周，如 2026-W43")
    parser.add_argument("--since", default=None, help="窗口起点（含）")
    parser.add_argument("--until", default=None, help="窗口终点（不含）")
    parser.add_argument("--db", default=None, help="state.db（默认 <VIGIL_HOME>/state.db）")
    parser.add_argument("--state-dir", default=None, help="报告输出根目录（默认 db 所在目录）")
    parser.add_argument("--format", choices=("facts", "md"), default="facts", help="输出格式（默认 facts）")
    parser.add_argument("--out", default=None, help="写文件；缺省打印 stdout")
    parser.add_argument(
        "--prose",
        default="auto",
        help=(
            "模型撰写的叙述（JSON：{summary, risks, next}，Markdown 片段）。默认 auto ="
            "读 <state-dir>/reports/<label>.prose.json；文件不存在则用确定性模板；传 'none' 强制模板。"
        ),
    )
    parser.add_argument(
        "--publish", choices=("none", "file", "bookstack"), default=None,
        help="发布目标（默认 none；weekly-oplog 蓝图按 ops.digest.publish 选择）",
    )
    parser.add_argument(
        "--exclude-job-id", action="append", default=None,
        help="额外排除某个 cron job 的会话（可重复，用于已删除的历史测试 job）",
    )
    parser.add_argument("--locale", default="zh", choices=("zh",), help="预留，只做 zh")


def _load_prose(state_dir: Path, label: str, spec: str) -> Optional[Dict[str, str]]:
    """读取模型撰写的叙述。

    ``spec``：``none`` 强制模板；``auto`` 读默认路径；其它当路径。
    文件不存在/解析失败一律回落模板（报告不能因为缺叙述而失败）。
    """
    spec = str(spec or "auto").strip()
    if spec.lower() == "none":
        return None
    path = Path(spec).expanduser() if spec.lower() != "auto" else (
        state_dir / "reports" / f"{REPORT_PREFIX}{label}.prose.json"
    )
    if not path.is_file():
        if spec.lower() != "auto":
            print(f"digest: prose 文件不存在，回落模板：{path}", file=sys.stderr)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"digest: prose 文件无法解析，回落模板：{path}（{exc}）", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"digest: prose 顶层必须是对象，回落模板：{path}", file=sys.stderr)
        return None
    return {k: str(data.get(k) or "") for k in ("summary", "risks", "next")}


def _run(args, *, job_id: Optional[str] = None, job_name: Optional[str] = None) -> int:
    now = datetime.now(SHANGHAI)
    try:
        since, until, label = parse_window(args, now)
    except ValueError as exc:
        print(f"digest: {exc}", file=sys.stderr)
        return 2

    home = Path(os.environ.get("VIGIL_HOME") or (Path.home() / ".vigil"))
    db = Path(args.db).expanduser() if args.db else home / "state.db"
    if not db.is_file():
        print(f"digest: state.db 不存在：{db}", file=sys.stderr)
        return 3
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else db.resolve().parent

    try:
        facts = collect(
            db, since, until,
            extra_job_ids=args.exclude_job_id,
            job_id=job_id,
            job_name=job_name,
        )
    except (sqlite3.Error, OSError) as exc:
        print(f"digest: 无法读取 state.db：{exc}", file=sys.stderr)
        return 3

    target = str(args.publish or "none").strip().lower()
    prose = _load_prose(state_dir, label, args.prose)

    if target == "none":
        if args.format == "facts":
            output = json.dumps(facts, ensure_ascii=False, indent=2)
        else:
            output = render_md(facts, prose)
        if args.out:
            path = Path(args.out).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output, encoding="utf-8")
            print(f"written: {path}")
        else:
            print(output)
        return 0

    if args.format != "md":
        print("digest: --publish 需要配合 --format md（发布的是渲染稿）", file=sys.stderr)
        return 2

    markdown = render_md(facts, prose)
    default_path = state_dir / "reports" / f"{REPORT_PREFIX}{label}.md"
    report_path = Path(args.out).expanduser() if args.out else default_path
    name = report_path.stem
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        existed = report_path.exists()
        report_path.write_text(markdown, encoding="utf-8")
        if target == "file":
            result = {
                "action": "updated" if existed else "created",
                "target": "file",
                "path": str(report_path),
            }
        else:
            result = publish_markdown(
                markdown,
                name=name,
                state_dir=state_dir,
                target=target,
                report_path=report_path,
            )
    except DigestPublishError as exc:
        print(f"digest: 发布失败：{exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"digest: 写出报告失败：{exc}", file=sys.stderr)
        return 4

    action = str(result.get("action") or "unknown")
    page_id = result.get("page_id")
    url = result.get("url")
    fields = [f"action={action}"]
    if page_id is not None:
        fields.append(f"page_id={page_id}")
    if url:
        fields.append(f"url={url}")
    fields.append(f"md={report_path}")
    print(" ".join(fields))
    return 0


def run_from_args(args, *, job_id: Optional[str] = None,
                  job_name: Optional[str] = None) -> int:
    """供 hermes_cli 子命令直接调用（args 已是 argparse.Namespace）。"""
    return _run(args, job_id=job_id, job_name=job_name)


def build_parser(prog: str = "vigil digest") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Vigil 作业日志汇总（我做了什么 → 改了什么）",
    )
    add_arguments(parser)
    return parser


def main(argv: Optional[Sequence[str]] = None, *, job_id: Optional[str] = None,
         job_name: Optional[str] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return _run(args, job_id=job_id, job_name=job_name)


if __name__ == "__main__":
    sys.exit(main())
