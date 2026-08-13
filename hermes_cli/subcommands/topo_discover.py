"""``vigil topo-discover`` subcommand parser.

Attaches the guided topology discovery command to the top-level CLI. The
handler is injected (same pattern as every other subcommand) so the parser
module stays light and ``main`` is not imported at build time.
"""

from __future__ import annotations

from typing import Callable


def _dispatch(args, cmd_topo_discover=None):
    """把顶层 Namespace 还原为 CLI argv 后交给独立 main（避免 main.py 重复拼参数）。"""
    from hermes_cli.topo_discover import main as topo_discover_main

    argv = []
    if getattr(args, "host", None):
        argv += ["-H", args.host]
    if getattr(args, "hosts", None):
        argv += ["--hosts", args.hosts]
    argv += ["-e", args.env]
    if getattr(args, "user", None):
        argv += ["-u", args.user]
    if getattr(args, "key", None):
        argv += ["-k", args.key]
    for flag, dest in (
        ("--password", "password"),
        ("--password-stdin", "password_stdin"),
        ("--key-passphrase", "key_passphrase"),
        ("--sudo-password", "sudo_password"),
        ("--skip-unidentified", "skip_unidentified"),
        ("--dry-run", "dry_run"),
        ("--force", "force"),
        ("--yes", "yes"),
    ):
        if getattr(args, dest, False):
            argv.append(flag)
    return topo_discover_main(argv)


def build_topo_discover_parser(subparsers, *, cmd_topo_discover: Callable) -> None:
    """Attach the ``topo-discover`` subcommand to ``subparsers``."""
    topo_discover_parser = subparsers.add_parser(
        "topo-discover",
        help="SSH 自动发现主机拓扑（docker/k8s/端口/GPU）并生成 v0.2 拓扑片段",
        description=(
            "SSH 进主机扫描 docker compose / k8s / 监听端口 / GPU，自动生成 "
            "schema v0.2 拓扑片段（第一层 host 行 + 第二层服务索引 + 第三层详情草案）。"
            "发现结果带 needs_review=true，人工确认后才落盘（--dry-run 只预览）。"
            "短选项：-H/--host、-e/--env、-u/--user、-k/--key；-h 仍为帮助。"
        ),
    )
    topo_discover_parser.add_argument(
        "-H", "--host",
        help="目标主机 IP/主机名；支持逗号列表与 [3-8] 区间展开",
    )
    topo_discover_parser.add_argument(
        "--hosts",
        help="主机列表文件（每行一个 host，忽略空白行/注释）",
    )
    topo_discover_parser.add_argument(
        "-e", "--env", required=True,
        help="目标环境（test/uat/prod/自定义名）",
    )
    topo_discover_parser.add_argument(
        "-u", "--user", default=None,
        help="SSH 用户（默认 root）",
    )
    topo_discover_parser.add_argument(
        "-k", "--key", default=None,
        help="SSH 私钥路径（默认走 ssh-agent；凭据经安全注入，命令行/日志不出现明文）",
    )
    topo_discover_parser.add_argument(
        "--password", action="store_true",
        help="交互提示输入 SSH 密码（不落 argv）",
    )
    topo_discover_parser.add_argument(
        "--password-stdin", action="store_true",
        help="从 stdin 读取 SSH 密码（管道场景）",
    )
    topo_discover_parser.add_argument(
        "--key-passphrase", action="store_true",
        help="交互提示输入加密私钥 passphrase（需配合 --key）",
    )
    topo_discover_parser.add_argument(
        "--sudo-password", action="store_true",
        help="交互提示输入 sudo 密码，用于 docker/kubectl 等提权探测",
    )
    topo_discover_parser.add_argument(
        "--skip-unidentified", action="store_true",
        help="跳过 ss 端口扫描生成的 unidentified 服务",
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
    topo_discover_parser.set_defaults(func=_dispatch)
