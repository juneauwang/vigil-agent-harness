"""``vigil digest`` subcommand parser.

Attaches the built-in work-log digest command to the top-level CLI. The
argument surface lives in ``ops.digest.add_arguments`` so the standalone
collector, cron builtin runner and CLI cannot drift.
"""

from __future__ import annotations

from typing import Callable

from ops.digest import add_arguments


def build_digest_parser(subparsers, *, cmd_digest: Callable) -> None:
    """Attach the ``digest`` subcommand to ``subparsers``."""
    digest_parser = subparsers.add_parser(
        "digest",
        help="作业周报：按「我提的任务 → 实际做的变更」汇总一个时间窗",
        description=(
            "内置作业周报：从 state.db 只读取数，按任务/变更明细汇总；"
            "可输出 facts JSON 或 Markdown，并可发布到 BookStack。"
            "发布配置在 config.yaml 的 ops.digest.publish；凭证在 "
            "credential_vault（vigil credential store <name>）。"
        ),
    )
    add_arguments(digest_parser)
    digest_parser.set_defaults(func=cmd_digest)
