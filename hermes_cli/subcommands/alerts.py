"""``vigil alerts`` subcommand parser（batch87，OPS-DELTA #103）。

Surface:
- ``vigil alerts`` / ``vigil alerts list`` — 活跃告警 + 逐条 runbook 处置建议
  （匹配 runbook/置信度/匹配依据/次优；文本或 --json）。
- ``vigil alerts history`` — 回看 triage 审计（runtime/alert_triage.jsonl，
  dogfood 调触发词/阈值的数据来源）。
"""

from __future__ import annotations

from typing import Callable


def build_alerts_parser(subparsers, *, cmd_alerts: Callable) -> None:
    """Attach the ``alerts`` subcommand to ``subparsers``."""
    alerts_parser = subparsers.add_parser(
        "alerts",
        help="活跃告警 + runbook 处置建议（建议闭环，只读）",
        description=(
            "告警→runbook 处置建议：拉 Alertmanager 活跃告警，逐条匹配最可能的 "
            "runbook（triggers 触发词精确命中 > 模糊评分），显示 runbook/置信度/"
            "匹配依据与次优 SOP。匹配结果仅供建议，绝不自动执行——执行需人工确认后 "
            "走 runbook_execute / Web 既有执行链（矩阵 + 审批门）。`history` 回看 "
            "triage 审计（每次调用一条，runtime/alert_triage.jsonl）。"
        ),
    )
    alerts_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（机器可读）",
    )
    sub = alerts_parser.add_subparsers(dest="alerts_command", metavar="<subcommand>")

    list_parser = sub.add_parser(
        "list",
        help="列出活跃告警 + 处置建议（默认动作）",
        description=(
            "列出 Alertmanager 活跃告警与每条的建议（matched/runbook/confidence/"
            "matched_by/次优；无匹配告警给出提示）。--json 出完整 JSON。"
        ),
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（机器可读）",
    )
    list_parser.set_defaults(func=cmd_alerts)

    history_parser = sub.add_parser(
        "history",
        help="回看 triage 审计记录（runtime/alert_triage.jsonl）",
        description=(
            "回看每次 alert triage 调用落盘的审计（时间/告警数/匹配数 + 逐条："
            "告警/是否命中/命中的 runbook/匹配方式/关键词）——dogfood 调触发词与"
            "匹配阈值的数据来源，不是合规台账。"
        ),
    )
    history_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（机器可读）",
    )
    history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="最多显示条数（默认 20）",
    )
    history_parser.set_defaults(func=cmd_alerts)

    # auto-dispatch（任务25）：告警→授权 runbook 自动执行（opt-in 默认关）
    auto_parser = sub.add_parser(
        "auto-dispatch",
        help="注册/注销告警自动派发（opt-in 默认关；仅执行 alert_auto_run 授权的 runbook）",
        description=(
            "自动值守最后一环：活跃告警匹配到 **显式授权**（alert_auto_run: true，"
            "随资产审批落盘）的 runbook 时，经定时执行同一豁免通道无人值守执行"
            "（预审标记 + 内容哈希在引擎侧 fail-closed），事后审计。未授权命中维持"
            "建议闭环。--schedule 注册周期 cron no_agent job（task18 topo-sync 同款，"
            "无新守护进程）；实际派发还需 config ops.alerts.auto_dispatch.enabled: true"
            "（每次 tick 重读，配置关闭即停）。"
        ),
    )
    auto_parser.add_argument(
        "--schedule",
        help="注册周期 cron job（如 5m/10m/1h；off = 注销）。opt-in：未注册 = 不派发",
    )
    auto_parser.set_defaults(func=cmd_alerts)

    alerts_parser.set_defaults(func=cmd_alerts)
