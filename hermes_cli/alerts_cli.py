"""``vigil alerts`` 运行逻辑（batch87，OPS-DELTA #103）。

告警→runbook 处置建议闭环的 CLI 面：列出 Alertmanager 活跃告警 + 每条的建议
（匹配 runbook/置信度/匹配依据/次优），以及回看 triage 审计（history）。
匹配器只产建议，执行 = 走既有 runbook_execute 全链（矩阵/审批门/审计）——CLI
这里同样不提供执行入口（建议后由用户在 agent 会话/Web 确认执行）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from hermes_cli.monitoring import MonitoringUnavailable, MonitoringUpstreamError

_STATUS_OK = 0
_STATUS_ERROR = 1


def _json_out(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return _STATUS_OK


def _fmt_disposition_text(disp: Dict[str, Any]) -> str:
    if not disp.get("matched"):
        return "无匹配 runbook" + (
            f"（{disp.get('hint')}）" if disp.get("hint") else ""
        )
    confidence = disp.get("confidence")
    matched_by = disp.get("matched_by")
    label = {"high": "高置信", "medium": "中置信"}.get(confidence, confidence or "")
    basis = "触发词命中" if matched_by == "trigger" else "模糊匹配"
    title = disp.get("title")
    name = disp.get("runbook")
    head = f"→ {name}（{label} · {basis}"
    if disp.get("matched_keyword"):
        head += f"「{disp.get('matched_keyword')}」"
    head += "）"
    if title:
        head += f" · {title}"
    alts = disp.get("alternatives") or []
    if alts:
        head += "；次优: " + ", ".join(a.get("name") or "" for a in alts)
    return head


def _print_alerts_text(payload: Dict[str, Any]) -> None:
    alerts: List[Dict[str, Any]] = payload.get("alerts") or []
    print(
        f"活跃告警: {payload.get('count', 0)}"
        f"（{payload.get('matched_count', 0)} 条有 runbook 建议）"
    )
    for a in alerts:
        sev = a.get("severity") or "info"
        inst = a.get("instance") or ""
        line = f"- [{sev}] {a.get('alertname') or '未知告警'}"
        if inst:
            line += f" · instance={inst}"
        if a.get("summary"):
            line += f"\n  summary: {a['summary']}"
        line += "\n  建议处置: " + _fmt_disposition_text(a.get("disposition") or {})
        print(line)
    print("\n提示：以上为处置建议（仅供建议，不自动执行）。确认后执行请走 "
          "runbook_execute（agent）/ Web 监控页 / 既有执行链（矩阵 + 审批门）。")


def _run_list(args) -> int:
    from tools.alert_runbook import triage_active_alerts

    try:
        payload = triage_active_alerts()
    except MonitoringUnavailable as exc:
        print(f"✗ {exc}")
        return _STATUS_ERROR
    except MonitoringUpstreamError as exc:
        print(f"✗ Alertmanager 上游错误: {exc}")
        return _STATUS_ERROR
    if getattr(args, "json", False):
        return _json_out(payload)
    _print_alerts_text(payload)
    return _STATUS_OK


def _run_history(args) -> int:
    from tools.alert_runbook import recent_alert_triage

    limit = int(getattr(args, "limit", 20) or 20)
    rows = recent_alert_triage(limit=limit)
    if getattr(args, "json", False):
        return _json_out(rows)
    print(f"最近 triage 审计（runtime/alert_triage.jsonl）: {len(rows)} 条")
    for row in rows:
        print(
            f"- {row.get('ts')} · 告警 {row.get('alert_count', 0)} 条"
            f"（匹配 {row.get('matched_count', 0)}）"
        )
        for e in row.get("entries") or []:
            hit = e.get("runbook") or "无匹配"
            detail = (
                f" → {hit}"
                + (f"（{e.get('confidence')}/{e.get('matched_by')}）" if e.get("matched") else "")
            )
            print(f"    {e.get('alertname') or '?'} [{e.get('severity') or '?'}]{detail}")
    return _STATUS_OK


def run_alerts_command(args) -> int:
    """`vigil alerts` 分发：默认/显式 list 列活跃告警 + 建议；history 回看审计。"""
    sub = str(getattr(args, "alerts_command", "") or "").strip()
    if sub == "history":
        return _run_history(args)
    return _run_list(args)
