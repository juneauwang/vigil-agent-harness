"""``vigil alerts`` 运行逻辑（batch87，OPS-DELTA #103）。

告警→runbook 处置建议闭环的 CLI 面：列出 Alertmanager 活跃告警 + 每条的建议
（匹配 runbook/置信度/匹配依据/次优），以及回看 triage 审计（history）。
匹配器只产建议，执行 = 走既有 runbook_execute 全链（矩阵/审批门/审计）——CLI
这里同样不提供执行入口（建议后由用户在 agent 会话/Web 确认执行）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from hermes_cli.i18n import t
from hermes_cli.monitoring import MonitoringUnavailable, MonitoringUpstreamError

_STATUS_OK = 0
_STATUS_ERROR = 1


def _json_out(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return _STATUS_OK


def _fmt_disposition_text(disp: Dict[str, Any]) -> str:
    if not disp.get("matched"):
        return t("alerts.no_match", "无匹配 runbook") + (
            t("alerts.hint_parens", "（{hint}）", hint=disp.get("hint")) if disp.get("hint") else ""
        )
    confidence = disp.get("confidence")
    matched_by = disp.get("matched_by")
    label = {
        "high": t("alerts.conf_high", "高置信"),
        "medium": t("alerts.conf_medium", "中置信"),
    }.get(confidence, confidence or "")
    basis = (
        t("alerts.basis_trigger", "触发词命中")
        if matched_by == "trigger"
        else t("alerts.basis_fuzzy", "模糊匹配")
    )
    title = disp.get("title")
    name = disp.get("runbook")
    head = t("alerts.match_head", "→ {name}（{label} · {basis}", name=name, label=label, basis=basis)
    if disp.get("matched_keyword"):
        head += t("alerts.match_keyword", "「{keyword}」", keyword=disp.get("matched_keyword"))
    head += t("alerts.match_close", "）")
    if title:
        head += t("alerts.match_title", " · {title}", title=title)
    alts = disp.get("alternatives") or []
    if alts:
        names = ", ".join(a.get("name") or "" for a in alts)
        head += t("alerts.alternatives", "；次优: {names}", names=names)
    return head


def _print_alerts_text(payload: Dict[str, Any]) -> None:
    alerts: List[Dict[str, Any]] = payload.get("alerts") or []
    print(t(
        "alerts.list_header",
        "活跃告警: {count}（{matched} 条有 runbook 建议）",
        count=payload.get("count", 0),
        matched=payload.get("matched_count", 0),
    ))
    for a in alerts:
        sev = a.get("severity") or "info"
        inst = a.get("instance") or ""
        line = t("alerts.alert_line", "- [{sev}] {alertname}",
                 sev=sev, alertname=a.get("alertname") or t("alerts.unknown_alert", "未知告警"))
        if inst:
            line += f" · instance={inst}"
        if a.get("summary"):
            line += f"\n  summary: {a['summary']}"
        line += t("alerts.disposition_label", "\n  建议处置: ") + _fmt_disposition_text(a.get("disposition") or {})
        print(line)
    print(t(
        "alerts.list_footer",
        "\n提示：以上为处置建议（仅供建议，不自动执行）。确认后执行请走 "
        "runbook_execute（agent）/ Web 监控页 / 既有执行链（矩阵 + 审批门）。",
    ))


def _run_list(args) -> int:
    from tools.alert_runbook import triage_active_alerts

    try:
        payload = triage_active_alerts()
    except MonitoringUnavailable as exc:
        print(f"✗ {exc}")
        return _STATUS_ERROR
    except MonitoringUpstreamError as exc:
        print(t("alerts.upstream_error", "✗ Alertmanager 上游错误: {exc}", exc=exc))
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
    print(t("alerts.history_header",
            "最近 triage 审计（runtime/alert_triage.jsonl）: {n} 条", n=len(rows)))
    for row in rows:
        print(t(
            "alerts.history_row",
            "- {ts} · 告警 {total} 条（匹配 {matched}）",
            ts=row.get("ts"),
            total=row.get("alert_count", 0),
            matched=row.get("matched_count", 0),
        ))
        for e in row.get("entries") or []:
            hit = e.get("runbook") or t("alerts.no_match", "无匹配")
            detail = (
                f" → {hit}"
                + (t("alerts.history_conf", "（{confidence}/{matched_by}）",
                     confidence=e.get("confidence"), matched_by=e.get("matched_by"))
                   if e.get("matched") else "")
            )
            print(f"    {e.get('alertname') or '?'} [{e.get('severity') or '?'}]{detail}")
    return _STATUS_OK


def run_alerts_command(args) -> int:
    """`vigil alerts` 分发：默认/显式 list 列活跃告警 + 建议；history 回看审计。"""
    sub = str(getattr(args, "alerts_command", "") or "").strip()
    if sub == "history":
        return _run_history(args)
    return _run_list(args)
