"""``vigil trajectory`` —— 运行轨迹审计查询/回放（批次二十三）。

事件级运行轨迹（agent/trajectory.py record_event 落盘 <数据根>/trajectory/
<session_id>.jsonl，append-only）：谁在什么时间执行了什么命令、结果如何、
审批如何裁决。运维审计合规（事故复盘时看"agent 当时看到了什么、为什么这么
决策、执行了什么"）+ 卡顿点识别（--replay 的事件间间隔）。

子命令：
    list                       列出所有轨迹文件（session + 事件数 + 时间跨度）
    show <session_id>          按 seq 打印事件流（--type 过滤 / --replay 时间线）
    search <query>             跨 session 搜 action/result
    prune --before <ISO>       删最后活动早于指定时间的轨迹文件（保留期管理）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from agent.trajectory import get_trajectory_dir

# 展示截断：单行可 grep 优先，action/result 过长截断。
_ACTION_MAX = 120
_RESULT_MAX = 80

_ANSI = {
    "tool_call": "\033[36m",   # cyan
    "tool_result": "\033[32m",  # green
    "approval": "\033[33m",     # yellow
    "interrupt": "\033[31m",    # red
    "error": "\033[31m",        # red
    "trajectory_truncated": "\033[35m",  # magenta
    "session_end": "\033[35m",
    "reset": "\033[0m",
}


def _iter_event_files() -> List[Path]:
    d = get_trajectory_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"))


def _load_events(path: Path) -> List[Dict]:
    events: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    events.sort(key=lambda e: e.get("seq", 0))
    return events


def _trunc(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _type_label(event_type: str, color: bool) -> str:
    label = f"[{event_type}]"
    if color:
        return f"{_ANSI.get(event_type, '')}{label}{_ANSI['reset']}"
    return label


def _use_color() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _parse_iso(value: str) -> Optional[datetime]:
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
        # naive 输入（如 2026-08-01）按本地时区解释，保证与事件 ts（aware）
        # 可比较。
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def _cmd_list(args) -> int:
    files = _iter_event_files()
    if not files:
        print("No trajectory files under " + str(get_trajectory_dir()))
        return 0
    print(f"{'SESSION':<32} {'EVENTS':>6}  {'SPAN (first → last)'}")
    for f in files:
        events = _load_events(f)
        ts = [e.get("ts", "") for e in events if e.get("ts")]
        span = f"{ts[0]} → {ts[-1]}" if len(ts) >= 2 else (ts[0] if ts else "-")
        print(f"{f.stem:<32} {len(events):>6}  {span}")
    return 0


# ---------------------------------------------------------------------------
# show / replay
# ---------------------------------------------------------------------------

def _print_events(events: List[Dict]) -> None:
    color = _use_color()
    for e in events:
        ts = e.get("ts", "")
        type_ = e.get("type", "?")
        tool = e.get("tool", "")
        action = _trunc(e.get("action"), _ACTION_MAX)
        result = _trunc(e.get("result"), _RESULT_MAX)
        approval = e.get("approval")
        prefix = f"{ts} {_type_label(type_, color)}"
        if tool:
            prefix += f" {tool}"
        line = prefix
        if action:
            line += f" {action}"
        if result:
            line += f" → {result}"
        if approval and approval != type_:
            line += f" ({approval})"
        print(line)



def _cmd_show(args) -> int:
    sid = args.session_id
    safe_sid = re.sub(r"[^A-Za-z0-9._-]", "_", sid or "")
    path = get_trajectory_dir() / f"{safe_sid}.jsonl"
    if not path.is_file():
        print(f"No trajectory found for session {sid!r}", file=sys.stderr)
        return 1
    events = _load_events(path)
    if args.event_type:
        want = args.event_type
        events = [
            e for e in events
            if e.get("type") == want or e.get("tool") == want
        ]
    if not events:
        kind = args.event_type or "matching"
        print(f"No {kind} events for session {sid}")
        return 0
    _print_events(events)
    return 0


# ---------------------------------------------------------------------------
# search / prune
# ---------------------------------------------------------------------------

def _cmd_search(args) -> int:
    query = args.query
    hits = 0
    for f in _iter_event_files():
        for e in _load_events(f):
            action = e.get("action") or ""
            result = e.get("result") or ""
            if query in action or query in result:
                hits += 1
                type_ = e.get("type", "?")
                seq = e.get("seq", "?")
                print(f"{f.stem} #{seq} [{type_}] {_trunc(action, _ACTION_MAX)}")
    if hits == 0:
        print(f"No trajectory events match {query!r}")
    return 0


def _cmd_prune(args) -> int:
    before = _parse_iso(args.before)
    if before is None:
        print(
            f"Invalid --before time {args.before!r} (expected ISO 8601, "
            "e.g. 2026-08-01 or 2026-08-01T00:00:00)",
            file=sys.stderr,
        )
        return 2
    removed = 0
    for f in _iter_event_files():
        events = _load_events(f)
        last_ts = None
        for e in reversed(events):
            if e.get("ts"):
                last_ts = _parse_iso(e["ts"])
                break
        if last_ts is None:
            continue
        if last_ts < before:
            try:
                f.unlink()
                removed += 1
            except OSError as _exc:
                print(f"Failed to remove {f}: {_exc}", file=sys.stderr)
    print(f"Pruned {removed} trajectory file(s) with last event before "
          f"{before.isoformat()}")
    return 0


# ---------------------------------------------------------------------------
# parser / entry
# ---------------------------------------------------------------------------

def build_trajectory_parser(subparsers, *, cmd_trajectory: Callable) -> None:
    """Attach the ``trajectory`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "trajectory",
        help="运行轨迹审计：list/show/search/prune 事件级 session 日志",
        description=(
            "事件级运行轨迹审计/复盘（append-only session event log，落盘 "
            "<数据根>/trajectory/<session>.jsonl）：谁在什么时间执行了什么命令、"
            "结果如何、审批如何裁决。list 列出轨迹文件；show 按 seq 打印事件流"
            "（--type 过滤；--replay 紧凑时间线带事件间隔，识别卡顿点）；search "
            "跨 session 搜命令；prune --before 做保留期管理。"
        ),
    )
    sub = p.add_subparsers(dest="trajectory_command")

    p_list = sub.add_parser("list", help="列出所有轨迹文件（session + 事件数 + 时间跨度）")
    p_list.set_defaults(trajectory_command="list")

    p_show = sub.add_parser(
        "show", help="按 seq 打印某 session 的事件流",
        description=(
            "按 seq 打印事件流（时间正序）。--type terminal 只看命令事件（审计核心："
            "谁执行了什么命令）；--type approval 只看审批事件；--replay 紧凑时间线"
            "（事件间隔标注，识别审批 300s 等待/熔断锁等待等卡顿点）；--replay "
            "--approval 只看审批时间线。"
        ),
    )
    p_show.add_argument("session_id", help="会话 ID（trajectory list 的 SESSION 列）")
    p_show.add_argument("--type", dest="event_type", default=None,
                        help="只显示指定类型事件（terminal/approval/interrupt/error/...）")
    p_show.set_defaults(trajectory_command="show")

    p_search = sub.add_parser(
        "search", help="跨 session 搜索轨迹事件（grep action/result）")
    p_search.add_argument("query", help="搜索串（子串匹配 action 或 result）")
    p_search.set_defaults(trajectory_command="search")

    p_prune = sub.add_parser(
        "prune", help="删除最后活动早于指定时间的轨迹文件（保留期管理）")
    p_prune.add_argument("--before", required=True,
                         help="ISO 8601 时间：最后事件早于该时间的文件被删除（默认不自动删）")
    p_prune.set_defaults(trajectory_command="prune")

    p.set_defaults(func=cmd_trajectory)


def run(args) -> int:
    """argv Namespace → 子命令分发（测试/独立入口）。"""
    sub = getattr(args, "trajectory_command", None)
    if sub == "list":
        return _cmd_list(args)
    if sub == "show":
        return _cmd_show(args)
    if sub == "search":
        return _cmd_search(args)
    if sub == "prune":
        return _cmd_prune(args)
    print("usage: vigil trajectory <list|show <session_id>|search <query>|prune --before <ISO>>",
          file=sys.stderr)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    """独立入口（测试/直接调用）：argv → Namespace → run。"""
    parser = argparse.ArgumentParser(
        prog="vigil trajectory",
        description="运行轨迹审计：list/show/search/prune 事件级 session 日志",
    )
    sub = parser.add_subparsers(dest="trajectory_command")

    p_list = sub.add_parser("list", help="列出所有轨迹文件")
    p_list.set_defaults(trajectory_command="list")

    p_show = sub.add_parser("show", help="按 seq 打印事件流")
    p_show.add_argument("session_id")
    p_show.add_argument("--type", dest="event_type", default=None)
    p_show.set_defaults(trajectory_command="show")

    p_search = sub.add_parser("search", help="跨 session 搜索")
    p_search.add_argument("query")
    p_search.set_defaults(trajectory_command="search")

    p_prune = sub.add_parser("prune", help="保留期管理")
    p_prune.add_argument("--before", required=True)
    p_prune.set_defaults(trajectory_command="prune")

    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
