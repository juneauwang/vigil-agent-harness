"""``vigil approvals`` subcommand parser.

Follows the cron/security pattern: parser construction lives here, the
handler is injected by ``main.py`` so this module never imports ``main``
(cycle avoidance).

Surface:
- ``vigil approvals suggest``  — mine past approval decisions into
  config.yaml ``command_allowlist`` proposals (legacy allowlist miner).
- ``vigil approvals list``    — 自进化白名单全量（命令级，batch86 OPS-DELTA
  #102）：种子与自进化分开标注（模板/状态/计数/免问/沉淀时间/来源任务）。
- ``vigil approvals forget <template>`` — 撤销单条（种子可撤销；banned 条目仅
  本命令可解除）——删除后该命令回到弹审批状态。
- ``vigil approvals export``  — 一键导出（stdout YAML/JSON，备份/review）。
"""

from __future__ import annotations

from typing import Callable

from hermes_cli.i18n import t


def build_approvals_parser(subparsers, *, cmd_approvals: Callable) -> None:
    """Attach the ``approvals`` subcommand to ``subparsers``."""
    approvals_parser = subparsers.add_parser(
        "approvals",
        help=t("approvals.cmd_help",
               "审批工具：建议沉淀 / 自进化白名单查看-撤销-导出"),
        description=t(
            "approvals.cmd_desc",
            "审批相关工具。`vigil approvals suggest` 从会话库挖历史审批决策并提议 "
            "config.yaml command_allowlist 条目；`vigil approvals list / forget / "
            "export` 管理命令级自进化白名单（approval_memory.yaml，v0.1——基于使用"
            "历史的权限自进化：用户批准的只读命令模板沉淀为 active，下次跳过审批）。",
        ),
    )
    approvals_subparsers = approvals_parser.add_subparsers(
        dest="approvals_command",
        metavar="<subcommand>",
    )

    suggest_parser = approvals_subparsers.add_parser(
        "suggest",
        help="Propose command_allowlist entries from past approvals",
        description=(
            "Scan the session database for dangerous-classified commands "
            "that ran with user approval, rank the recurring patterns, and "
            "print a numbered allowlist proposal. Nothing is written unless "
            "--apply is given. Destructive classes (recursive delete, sudo, "
            "disk writes, credential edits, ...) are never proposed."
        ),
    )
    suggest_parser.add_argument(
        "--apply",
        dest="apply_indices",
        metavar="N[,M...]",
        help="Merge the numbered proposals (from a prior run) into "
        "command_allowlist in config.yaml",
    )
    suggest_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text",
    )
    suggest_parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="How far back to scan session history (default: 90; 0 = all)",
    )
    suggest_parser.add_argument(
        "--min-count",
        dest="min_count",
        type=int,
        default=2,
        help="Minimum approval count for a pattern to be proposed (default: 2)",
    )
    suggest_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of proposals to show (default: 20)",
    )
    suggest_parser.add_argument(
        "--db",
        help="Path to an alternate session database (default: ~/.vigil/state.db)",
    )
    suggest_parser.set_defaults(func=cmd_approvals)

    list_parser = approvals_subparsers.add_parser(
        "list",
        help=t("approvals.list_help",
               "列出命令级自进化白名单（种子 + 自进化，字段齐全）"),
        description=t(
            "approvals.list_desc",
            "列出 approval_memory.yaml 命令级自进化白名单全量：模板 / 状态（active="
            "沉淀完成下次跳过审批；pending=计数中；banned=被拒过永不自动沉淀；"
            "inactive=已撤销）/ 成功次数 / 免问 / 沉淀时间 / 来源任务；内置只读种子"
            "（lscpu/free/cat/...，冷启动起点）单独标注。",
        ),
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help=t("approvals.list_json_help", "以 JSON 输出（机器可读）"),
    )
    list_parser.set_defaults(func=cmd_approvals)

    forget_parser = approvals_subparsers.add_parser(
        "forget",
        help=t("approvals.forget_help",
               "撤销单条自进化白名单条目（删除后该命令回到弹审批状态）"),
        description=t(
            "approvals.forget_desc",
            "撤销单条命令级白名单（置 inactive、计数清零）：删除后该命令回到弹审批"
            "状态，重新批准 3 次可再沉淀。种子条目可撤销（forget lscpu 后 lscpu 回到"
            "弹审批）；banned 条目（被用户拒过）仅能通过本命令显式解除——不会自动"
            "复活。模板用归一化名（lscpu / cat / lsblk）。",
        ),
    )
    forget_parser.add_argument(
        "template",
        help=t("approvals.forget_template_help",
               "要撤销的命令模板（归一化名，如 lscpu / cat / lsblk）"),
    )
    forget_parser.set_defaults(func=cmd_approvals)

    export_parser = approvals_subparsers.add_parser(
        "export",
        help=t("approvals.export_help",
               "一键导出自进化白名单（stdout YAML；--json 出 JSON）"),
        description=t(
            "approvals.export_desc",
            "导出 approval_memory.yaml 全量到 stdout（默认 YAML，--json 出 JSON）："
            "条目含 模板/状态/计数/never_denied/user_opt_in/沉淀时间/来源任务，另附 "
            "内置种子清单——给用户备份/review（开源信誉：可查看/可撤销/可审计）。",
        ),
    )
    export_parser.add_argument(
        "--json",
        action="store_true",
        help=t("approvals.export_json_help", "以 JSON 输出"),
    )
    export_parser.set_defaults(func=cmd_approvals)

    approvals_parser.set_defaults(func=cmd_approvals)
