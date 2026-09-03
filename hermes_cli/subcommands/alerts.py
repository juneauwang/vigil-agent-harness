"""``vigil alerts`` subcommand parser（batch87，OPS-DELTA #103）。

Surface:
- ``vigil alerts`` / ``vigil alerts list`` — 活跃告警 + 逐条 runbook 处置建议
  （匹配 runbook/置信度/匹配依据/次优；文本或 --json）。
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
            "走 runbook_execute / Web 既有执行链（矩阵 + 审批门）。"
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

    alerts_parser.set_defaults(func=cmd_alerts)
