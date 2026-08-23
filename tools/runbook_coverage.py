"""runbook 覆盖率（OPS-DELTA #81）——确定性覆盖率 + 审计动作使用率。

- **确定性覆盖率**（设计 §7 未覆盖风险操作卡）：matrix.yaml ``{approve:
  required}`` 高危动作集（P3 矩阵，read-only）− runbooks（v0.1+v0.2）步骤
  动作集 = 未覆盖风险操作。高危 = 任一 env 档位 required（强制人工）。
- **使用率**（设计 §7/§八）：近 ``_USAGE_WINDOW_DAYS``（30）天
  trajectory/audit 事件里的命令 → P5 action classifier（tools/action_classifier，
  本模块不动）归类 → 动作使用频率表 → 与 runbooks 覆盖对比 → 缺口（高频未覆盖
  top N，提示「建议沉淀 runbook」）。

只读：矩阵 / runbook / 审计事件均不写不改；classifier 归类失败（unknown）
不计入常用动作。空矩阵/空 runbook/无审计 → 空态（覆盖率 0%，不崩）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 统计窗口 / 缺口阈值（OPS-DELTA #81 注明）。
_USAGE_WINDOW_DAYS = 30
_GAP_TOP_N = 5

# trajectory 事件里携带命令的 type（tool_call=工具调用、approval=审批记录、
# terminal=终端直跑记录）。
_CMD_EVENT_TYPES = frozenset({"tool_call", "approval", "terminal"})


def _runbooks_dir(home: Optional[Path]) -> Path:
    base = Path(home) if home is not None else Path(
        __import__("hermes_constants", fromlist=["get_hermes_home"]).get_hermes_home())
    return base / "runbooks"


def runbook_actions(home: Optional[Path] = None) -> Dict[str, List[str]]:
    """全部 runbooks（v0.1+v0.2）步骤动作集：{action: [runbook 名称]}。

    v0.2 取步骤 ``action`` 字段（含 rollback 场景步骤）；v0.1 步骤是 commands，
    用 P5 classifier 归类（unknown 不计）。坏 YAML/解析失败跳过。
    """
    from tools.action_classifier import classify_command
    from tools.runbook_tools import _load_runbook

    rdir = _runbooks_dir(home)
    out: Dict[str, List[str]] = {}
    if not rdir.is_dir():
        return out
    for path in sorted(rdir.glob("*.yaml")):
        name = path.stem
        try:
            data = _load_runbook(rdir.parent, name)
        except Exception:
            logger.debug("runbook coverage: skip unreadable %s", path)
            continue
        if not isinstance(data, dict):
            continue
        actions = set()

        def _collect_steps(steps: Any) -> None:
            for step in steps or []:
                if not isinstance(step, dict):
                    continue
                if step.get("action"):
                    actions.add(str(step["action"]))
                for cmd in step.get("commands") or []:
                    if not isinstance(cmd, str):
                        continue
                    act = classify_command(cmd).get("action")
                    if act and act != "unknown":
                        actions.add(act)

        _collect_steps(data.get("steps"))
        for rb in data.get("rollback") or []:
            if isinstance(rb, dict):
                _collect_steps(rb.get("steps"))
        for action in sorted(actions):
            out.setdefault(action, []).append(name)
    return out


def high_risk_actions(home: Optional[Path] = None) -> List[str]:
    """matrix.yaml ``{approve: required}`` 高危动作集（任一 env 命中 required）。

    只读矩阵（load_matrix_or_empty，热生效）；空矩阵 → 空列表。
    """
    from tools.matrix_data import LEVEL_REQUIRED, load_matrix_or_empty

    data = load_matrix_or_empty(home)
    matrix = data.get("matrix") or {}
    actions: set = set()
    for env, rows in matrix.items():
        if not isinstance(rows, dict):
            continue
        for action, level in rows.items():
            if level == LEVEL_REQUIRED:
                actions.add(str(action))
    return sorted(actions)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except Exception:
        return None


def _iter_cmd_events(home: Optional[Path], cutoff: datetime
                     ) -> List[Dict[str, Any]]:
    """近窗口内携带命令的审计事件（只读 trajectory/*.jsonl）。"""
    from hermes_cli.subcommands.trajectory import _iter_event_files, _load_events

    events: List[Dict[str, Any]] = []
    for path in _iter_event_files():
        for ev in _load_events(path):
            if ev.get("type") not in _CMD_EVENT_TYPES:
                continue
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < cutoff:
                continue
            cmd = ev.get("action")
            if isinstance(cmd, str) and cmd.strip():
                events.append({"ts": ts, "command": cmd})
    return events


def usage_snapshot(home: Optional[Path] = None, *,
                   days: int = _USAGE_WINDOW_DAYS,
                   gap_top_n: int = _GAP_TOP_N) -> Dict[str, Any]:
    """审计动作使用率 + 覆盖对比（小项3）。只读，空态不崩。"""
    from tools.action_classifier import classify_command

    actions = runbook_actions(home)
    cutoff = datetime.now().astimezone() - timedelta(days=int(days))
    events = _iter_cmd_events(home, cutoff)

    counts: Dict[str, int] = {}
    scanned = 0
    for ev in events:
        scanned += 1
        act = classify_command(ev["command"]).get("action")
        if not act or act == "unknown":
            continue
        counts[act] = counts.get(act, 0) + 1

    rows = [
        {
            "action": action,
            "use_count": counts.get(action, 0),
            "covered": action in actions,
            "runbooks": actions.get(action, []),
        }
        for action in sorted(counts, key=lambda a: (-counts[a], a))
    ]
    total_unique = len(rows)
    covered_unique = sum(1 for r in rows if r["covered"])
    gaps = [r for r in rows if not r["covered"]][: int(gap_top_n)]
    return {
        "window_days": int(days),
        "audit_events_scanned": scanned,
        "actions": rows,
        "total_unique": total_unique,
        "covered_unique": covered_unique,
        "coverage_pct": round(covered_unique * 100 / total_unique) if total_unique else 0,
        "gaps": gaps,
    }


def high_risk_snapshot(home: Optional[Path] = None) -> Dict[str, Any]:
    """确定性覆盖率（小项2）：高危 required 动作 − runbook 覆盖。"""
    actions = runbook_actions(home)
    high_risk = high_risk_actions(home)
    covered = [a for a in high_risk if a in actions]
    uncovered = [a for a in high_risk if a not in actions]
    return {
        "high_risk": high_risk,
        "total": len(high_risk),
        "covered": len(covered),
        "uncovered": uncovered,
        "coverage_pct": round(len(covered) * 100 / len(high_risk)) if high_risk else 0,
    }


def coverage_snapshot(home: Optional[Path] = None) -> Dict[str, Any]:
    """合并快照：确定性高危覆盖率 + 审计使用率（/api/runbook/coverage 载荷）。"""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "high_risk": high_risk_snapshot(home),
        "usage": usage_snapshot(home),
    }
