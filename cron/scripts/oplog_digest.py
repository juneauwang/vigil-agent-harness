#!/usr/bin/env python3
"""Vigil 作业日志（oplog）：按「我提的任务 → 实际做的变更」汇总一个时间窗。

与 weekly_change_digest.py 的区别：那份以「会话/计数」为中心，本脚本以**变更项**为中心——
每个会话先记用户提出的任务意图，再列出该会话里真正落地的东西（拓扑字段、文件、runbook、
技能、写类命令），供写周报和追踪变更使用。

用法：
    python cron/scripts/oplog_digest.py --db ~/.vigil/state.db --window 7d --format facts
    python cron/scripts/oplog_digest.py --window 2026-W43 --format md --out /tmp/r.md
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

SHANGHAI = timezone(timedelta(hours=8))
CRON_HDR = re.compile(r"^\[IMPORTANT: You are running as a scheduled cron job")
MAX_ASK = 140
MAX_DETAIL = 160
MAX_CHANGES_PER_TASK = 40
EXCLUDED_SOURCES = ("cron", "subagent")

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?\S+"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[=:]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9]{24,}\b"),
]


def redact(text: str) -> str:
    value = str(text or "")
    for pat in _SECRET_PATTERNS[:2]:
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

def parse_window(args, now: datetime) -> tuple[datetime, datetime, str]:
    if args.week:
        m = re.fullmatch(r"(\d{4})-W(\d{2})", args.week.strip())
        if not m:
            raise SystemExit("--week 需要形如 2026-W43")
        year, week = int(m.group(1)), int(m.group(2))
        monday = datetime.combine(date.fromisocalendar(year, week, 1), datetime.min.time(), SHANGHAI)
        return monday, monday + timedelta(days=7), f"{year}-W{week:02d}"
    if args.since:
        since = _parse_dt(args.since)
        until = _parse_dt(args.until) if args.until else now
        return since, until, _label(until - timedelta(seconds=1))
    m = re.fullmatch(r"(\d+)([dhm])", (args.window or "7d").strip())
    if not m:
        raise SystemExit("--window 需要形如 7d / 24h / 2w")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    since = now - delta
    return since, now, _label(now - timedelta(seconds=1))


def _parse_dt(text: str) -> datetime:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=SHANGHAI) if dt.tzinfo is None else dt.astimezone(SHANGHAI)
        except ValueError:
            continue
    raise SystemExit(f"无法解析时间：{text}")


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


def change_from_call(name: str, raw) -> dict | None:
    """把一次写类工具调用折成一条变更记录。"""
    a = _json(raw)
    if name == "topo_update":
        updates = a.get("updates") or {}
        detail = "; ".join(f"{k}={_fmt_value(v)}" for k, v in updates.items()) or "（无字段）"
        return {"kind": "拓扑", "target": a.get("entity") or "?", "detail": clean(detail, MAX_DETAIL),
                "why": clean(a.get("reason"), 100)}
    if name in ("write_file", "patch"):
        path = a.get("path") or a.get("file_path") or "?"
        action = "改" if name == "patch" else "写"
        head = a.get("new_string") if name == "patch" else a.get("content")
        return {"kind": "文件", "target": path, "detail": f"{action}：{purpose_of(head, path)}", "why": ""}
    if name == "runbook_execute":
        ctx = a.get("trigger_context") or {}
        return {"kind": "runbook", "target": f"{a.get('runbook') or '?'}",
                "detail": f"env={a.get('env') or '-'}", "why": clean(ctx.get("reason"), 100)}
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


_WRITE_VERB_RE = re.compile(
    r"(?i)(?:\b(?:sudo\s+)?(?:kubectl)\s+(?P<kubectl>apply|delete|create|edit|scale|rollout|patch|drain|cordon|taint|label|annotate))"
    r"|(?:\b(?:helm)\s+(?P<helm>install|upgrade|uninstall|rollback))"
    r"|(?:\bsystemctl\s+(?P<systemctl>start|stop|restart|reload|enable|disable|mask|unmask))"
    r"|(?:\bdocker(?:-compose|\s+compose)\s+(?P<compose>up|down|restart|start|stop|rm))"
    r"|(?:\bdocker\s+(?P<docker>run|rm|stop|start|restart|build|push|exec))"
    r"|(?:\b(?P<scp>scp|rsync)\b)"
    r"|(?:\b(?P<fileop>rm|mv|cp|chmod|chown|mkfs|truncate|dd)\s)"
    r"|(?:\bgit\s+(?P<git>push|reset|checkout|commit|merge|rebase)\b)"
    r"|(?:\b(?P<sql>DROP|ALTER|DELETE\s+FROM|UPDATE\s+\S+\s+SET)\b)"
    r"|(?:\b(?P<svc>reboot|shutdown)\b)"
    r"|(?P<tmux>-X\s+kill-session)"
)

_READ_ONLY_HEADS = (
    "echo", "printf", "ls", "cat", "head", "tail", "grep", "egrep", "rg", "awk", "jq",
    "sed", "test", "which", "whoami", "id", "date", "uptime", "df", "du", "free",
    "ss", "netstat", "ps", "env", "printenv", "curl", "wget", "ping", "dig", "nslookup",
)


def write_verb(command: str) -> str | None:
    """从命令里认出「写类动作」并归一成短标签；认不出返回 None（只读/探测）。"""
    text = str(command or "")
    if not text.strip():
        return None
    m = _WRITE_VERB_RE.search(text)
    if m:
        for group, value in m.groupdict().items():
            if value:
                return f"{group}:{value.lower()}"
    # kubectl 的 verb 可能被 shell 变量/别名包裹，退回分类器兜底（仅限明确写类档位）
    return None


def looks_read_only(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return True
    if write_verb(text):
        return False
    head = re.split(r"[\s;|&]", text, maxsplit=1)[0].lstrip("./")
    if head in _READ_ONLY_HEADS:
        return True
    # 含 -X POST/PUT/PATCH/DELETE 或 --data 的 curl 也算写
    if re.search(r"(?i)curl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|--data\b|-d\s|--upload-file|-F\s)", text):
        return False
    return True


def classify_terminal(command: str) -> dict | None:
    """写类终端命令 → 一条变更记录；只读/探测命令返回 None。"""
    if looks_read_only(command):
        return None
    verb = write_verb(command)
    return {"kind": "命令", "target": verb or "write", "detail": clean(command, 110), "why": ""}


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

def collect(db: Path, since: datetime, until: datetime, extra_job_ids: list[str] | None = None) -> dict:
    if not db.is_file():
        raise SystemExit(f"state.db 不存在：{db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lo, hi = since.timestamp(), until.timestamp()

    # 自身排除：cron 预跑会注入当前 job 的 id/name（cron/scheduler._run_job_script）。
    # - 有 env：只排除**当前 job** 的 cron 会话及其派生的 subagent（别把别的定时作业
    #   干的真活也抹掉，例如告警自动处置）。
    # - 无 env（手动跑）：排除全部 cron/subagent 会话——手动跑是为了记「我干了什么」。
    job_id = str(os.environ.get("VIGIL_CRON_JOB_ID") or "").strip()
    extra_excluded = {str(x).strip() for x in (extra_job_ids or []) if str(x).strip()}

    sessions = conn.execute(
        "select id, source, parent_session_id, started_at, ended_at, end_reason, message_count,"
        " tool_call_count, input_tokens, output_tokens from sessions"
        " where started_at >= ? and started_at < ? order by started_at", (lo, hi)).fetchall()

    prefixes = ([f"cron_{job_id}_"] if job_id else []) + [f"cron_{jid}_" for jid in extra_excluded]
    own_ids = {
        row["id"] for row in sessions
        if prefixes and row["source"] == "cron" and any(str(row["id"]).startswith(p) for p in prefixes)
    }
    # 级联：当前 job 会话派生的 subagent 也算自身
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
        if not job_id and row["source"] in EXCLUDED_SOURCES:
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

    by_kind: dict[str, int] = {}
    for task in tasks:
        for ch in task["changes"]:
            by_kind[ch["kind"]] = by_kind.get(ch["kind"], 0) + 1

    days: list[dict] = []
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
        "window": {"since": since.isoformat(), "until": until.isoformat(), "label": _label(until - timedelta(seconds=1))},
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

def render_md(facts: dict) -> str:
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
    if not facts["tasks"]:
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
    pending = [task for task in facts["tasks"] if (task["end_reason"] or "") not in ("cli_close", "turn_complete")]
    if not pending and not facts["gaps"]:
        lines.append("无。")
    for task in pending:
        lines.append(f"- {task['when']} {task['asks'][0] if task['asks'] else task['session']}：结束状态 {task['end_reason'] or '未知'}")
    for gap in facts["gaps"]:
        lines.append(f"- 数据缺：{gap}")
    lines += ["", "## 5. 下周期", "", "- 待补：由撰写者根据第 4 段给出 1-3 条。", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vigil 作业日志汇总（我做了什么 → 改了什么）")
    parser.add_argument("--db", default=None, help="state.db（默认 <VIGIL_HOME>/state.db）")
    parser.add_argument("--window", default=None, help="滚动窗口，如 7d / 24h（默认 7d）")
    parser.add_argument("--week", default=None, help="ISO 周，如 2026-W43")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--exclude-job-id", action="append", default=None,
                        help="额外排除某个 cron job 的会话（可重复，用于已删除的历史测试 job）")
    parser.add_argument("--format", choices=("facts", "md"), default="facts")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    home = Path(os.environ.get("VIGIL_HOME") or (Path.home() / ".vigil"))
    db = Path(args.db) if args.db else home / "state.db"
    since, until, _ = parse_window(args, datetime.now(SHANGHAI))
    facts = collect(db, since, until, extra_job_ids=args.exclude_job_id)
    output = json.dumps(facts, ensure_ascii=False, indent=2) if args.format == "facts" else render_md(facts)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
