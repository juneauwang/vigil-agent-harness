"""``vigil topo-sync`` subcommand parser.

拓扑自维护命令（任务18）：terraform.tfstate 同步源 + 漂移报告 + 周期重扫。
Dispatch 模式与 topo_discover 一致：把顶层 Namespace 传给独立 main，
本模块只做 argparse 声明，保持轻量。
"""

from __future__ import annotations

from typing import Callable


def _dispatch(args, cmd_topo_sync=None):
    from hermes_cli.topo_sync import main as topo_sync_main

    argv: list[str] = []
    for path in getattr(args, "tfstate", None) or []:
        argv += ["--tfstate", path]
    if getattr(args, "env", None):
        argv += ["--env", args.env]
    if getattr(args, "cluster", None):
        argv += ["--cluster", args.cluster]
    if getattr(args, "apply", False):
        argv.append("--apply")
    if getattr(args, "yes", False):
        argv.append("--yes")
    if getattr(args, "schedule", None) is not None:
        argv += ["--schedule", args.schedule]
    return topo_sync_main(argv)


def build_topo_sync_parser(subparsers, *, cmd_topo_sync: Callable) -> None:
    """Attach the ``topo-sync`` subcommand to ``subparsers``."""
    topo_sync_parser = subparsers.add_parser(
        "topo-sync",
        help="拓扑自维护：terraform.tfstate 同步 + 漂移报告 + 周期重扫（opt-in）",
        description=(
            "解析 terraform.tfstate（terraform show -json 形态，alicloud/aws/"
            "azurerm 主机资源族）对照现有拓扑产出漂移报告（新增/字段变化/消失"
            "待复核）。默认只报告不落盘；--apply 只追加新增行（同名既有行绝不"
            "覆盖，消失行绝不自动删除）。--schedule 注册周期重扫（复用 cron "
            "调度器，opt-in 默认关）。"
        ),
    )
    topo_sync_parser.add_argument(
        "--tfstate", action="append",
        help="tfstate/terraform show -json JSON 文件路径（可重复；缺省读 "
             "config ops.topology.tfstate_paths）",
    )
    topo_sync_parser.add_argument(
        "--env", help="tfstate 无 env tag 时的缺省环境（tag > 本参数 > local）",
    )
    topo_sync_parser.add_argument(
        "--cluster", help="新 host 行的 cluster 标记（可选）",
    )
    topo_sync_parser.add_argument(
        "--apply", action="store_true",
        help="把新增 host 行写入 topology.yaml（同名既有行绝不覆盖；缺省只报告）",
    )
    topo_sync_parser.add_argument(
        "--yes", action="store_true", help="跳过 --apply 的交互确认",
    )
    topo_sync_parser.add_argument(
        "--schedule",
        help="注册周期重扫 cron job（如 24h/1d；off = 注销）。opt-in：未注册 = 不重扫",
    )
    topo_sync_parser.set_defaults(func=_dispatch)
