"""``vigil topo-discover`` subcommand parser.

Attaches the guided topology discovery command to the top-level CLI. The
handler is injected (same pattern as every other subcommand) so the parser
module stays light and ``main`` is not imported at build time.
"""

from __future__ import annotations

from typing import Callable


def build_topo_discover_parser(subparsers, *, cmd_topo_discover: Callable) -> None:
    """Attach the ``topo-discover`` subcommand to ``subparsers``."""
    topo_discover_parser = subparsers.add_parser(
        "topo-discover",
        help="SSH 自动发现主机拓扑（docker/k8s/端口/GPU）并生成 v0.2 拓扑片段",
        description=(
            "SSH 进主机扫描 docker compose / k8s / 监听端口 / GPU，自动生成 "
            "schema v0.2 拓扑片段（第一层 host 行 + 第二层服务索引 + 第三层详情草案）。"
            "发现结果带 needs_review=true，人工确认后才落盘（--dry-run 只预览）。"
        ),
    )
    topo_discover_parser.add_argument(
        "--host", required=True,
        help="目标主机 IP/主机名（作为 host endpoint）",
    )
    topo_discover_parser.add_argument(
        "--env", required=True,
        help="目标环境（test/uat/prod/自定义名）",
    )
    topo_discover_parser.add_argument(
        "--user", default=None,
        help="SSH 用户（默认 root）",
    )
    topo_discover_parser.add_argument(
        "--key", default=None,
        help="SSH 私钥路径（默认走 ssh-agent；密码经提示输入并安全存储）",
    )
    topo_discover_parser.add_argument(
        "--dry-run", action="store_true",
        help="只展示发现结果，不落盘",
    )
    topo_discover_parser.add_argument(
        "--force", action="store_true",
        help="覆盖已存在的 host 条目（默认拒绝覆盖）",
    )
    topo_discover_parser.add_argument(
        "--yes", action="store_true",
        help="跳过交互确认（配合 --force 可非交互落盘）",
    )
    topo_discover_parser.set_defaults(func=cmd_topo_discover)
