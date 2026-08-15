"""``vigil watch`` subcommand parser.

Attaches the watch collector service management command to the top-level CLI.
The handler is injected (same pattern as every other subcommand) so the parser
module stays light and ``main`` is not imported at build time.
"""

from __future__ import annotations

from typing import Callable


def build_watch_parser(subparsers, *, cmd_watch: Callable) -> None:
    """Attach the ``watch`` subcommand to ``subparsers``."""
    watch_parser = subparsers.add_parser(
        "watch",
        help="值守采集服务（vigil-watch.systemd --user）管理",
        description=(
            "值守层第一层：systemd --user 常驻采集（每 5 分钟拉 alertmanager → "
            "有告警写 ~/.vigil/watch/inbox/）。采集是确定性代码不需要 LLM；"
            "分析播报由 agent 会话消费 inbox（watch_digest）。"
        ),
    )
    watch_subparsers = watch_parser.add_subparsers(dest="watch_command")

    install = watch_subparsers.add_parser(
        "install", help="安装并启动 vigil-watch.service（enable --now 开机自启）"
    )
    install.set_defaults(watch_action="install")

    uninstall = watch_subparsers.add_parser(
        "uninstall", help="停止、禁用并删除 vigil-watch.service"
    )
    uninstall.set_defaults(watch_action="uninstall")

    status = watch_subparsers.add_parser(
        "status", help="真实状态：systemd 服务状态 + 上次采集 + inbox 未处理数"
    )
    status.set_defaults(watch_action="status")

    watch_parser.set_defaults(func=cmd_watch, watch_command=None, watch_action="status")
