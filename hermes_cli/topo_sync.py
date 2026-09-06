"""``vigil topo-sync`` —— 拓扑自维护：tfstate 同步 + 漂移报告 + 周期重扫（任务18）。

三条能力，全部复用既有机制、零新守护进程：

1. **tfstate 同步源**：解析 ``terraform show -json`` / tfstate JSON（云主机
   资源族 → v0.4 host 行），对照现有拓扑产出漂移报告（added/changed/vanished/
   unchanged）。**默认只报告不落盘**；``--apply`` 只追加新增行（同名既有行
   绝不覆盖，消失行绝不自动删除——人工 review 流）。
2. **漂移报告落点**：stdout + ``<home>/runtime/topo_drift.json``（最新一份）
   + trajectory 审计事件（type=topo_drift，best-effort）。
3. **周期重扫**：复用现有 cron 调度器（与 tools/runbook_schedule.py 同款
   注册模式）——``--schedule <interval>`` 注册一个 name=topo_sync 的
   ``no_agent`` job，脚本即本命令的只读报告路径（报告 + 审计，不写拓扑）；
   ``--schedule off`` 注销。未注册 = 不重扫（opt-in，默认关）。

用法：
    vigil topo-sync                                  # 读 config ops.topology.tfstate_paths → 漂移报告
    vigil topo-sync --tfstate prod.tfstate --env prod [--apply] [--yes]
    vigil topo-sync --schedule 24h                   # 注册每日重扫（幂等）
    vigil topo-sync --schedule off                   # 注销
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_TOPO_SYNC_JOB_NAME = "topo_sync"


def _active_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _config_topology() -> Dict[str, Any]:
    """``ops.topology`` 配置段（缺省 = 空段，键全走代码内默认）。"""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = ((cfg.get("ops") or {}).get("topology") or {})
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _resolve_tfstate_paths(args) -> List[str]:
    """--tfstate 优先；未给则回落 config ``ops.topology.tfstate_paths``。"""
    flagged = [str(p).strip() for p in (getattr(args, "tfstate", None) or [])]
    flagged = [p for p in flagged if p]
    if flagged:
        return flagged
    configured = _config_topology().get("tfstate_paths")
    if isinstance(configured, list):
        return [str(p).strip() for p in configured
                if isinstance(p, str) and p.strip()]
    return []


def _read_rows(tfstate_paths: List[str], env_default: str,
               cluster: str = "") -> List[Dict[str, Any]]:
    """读全部 tfstate 文件 → 解析 → 按名去重合并（同名片可覆盖 env 缺省）。"""
    from tools.topo_discovery import parse_tfstate

    rows: Dict[str, Dict[str, Any]] = {}
    for path in tfstate_paths:
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"tfstate 文件不存在：{path}")
        parsed = parse_tfstate(p.read_text(encoding="utf-8"))
        if not parsed:
            print(f"· {path}: 未发现可映射的云主机资源（忽略）")
            continue
        for row in parsed:
            row = dict(row)
            # env 优先级：tfstate tag > --env > local
            if not row.get("env"):
                row["env"] = env_default or "local"
            if cluster and not row.get("cluster"):
                row["cluster"] = cluster
            rows[row["name"]] = row
    return [rows[name] for name in sorted(rows)]


def _print_report(report: Dict[str, Any]) -> None:
    print("===== 拓扑漂移报告（仅建议，不自动应用） =====")
    print(f"新发现（added）: {len(report['added'])}")
    for row in report["added"]:
        print(f"  + {row['name']}  env={row.get('env') or '-'}  "
              f"endpoint={row.get('endpoint') or '-'}")
    print(f"字段变化（changed）: {len(report['changed'])}")
    for item in report["changed"]:
        for ch in item["changes"]:
            print(f"  ~ {item['name']}.{ch['field']}: {ch['old']!r} → {ch['new']!r}")
    print(f"消失待复核（vanished）: {len(report['vanished'])}")
    for item in report["vanished"]:
        print(f"  - {item['name']}  endpoint={item.get('endpoint') or '-'}"
              "  [不自动删除——请人工确认后用 topo_update 处理]")
    print(f"无变化（unchanged）: {report['unchanged']}  "
          f"（本次解析 {report['total_discovered']} 行）")
    print("==============================================")


def _record_drift_event(report: Dict[str, Any]) -> None:
    """trajectory 审计（type=topo_drift）；失败仅记日志，不阻断。"""
    try:
        from agent.trajectory import record_event
        record_event(
            type="topo_drift",
            session_id="topo-sync",
            action="report",
            meta={
                "source": "terraform",
                "added": [r["name"] for r in report["added"]],
                "changed": [c["name"] for c in report["changed"]],
                "vanished": [v["name"] for v in report["vanished"]],
                "unchanged": report["unchanged"],
                "total_discovered": report["total_discovered"],
            },
        )
    except Exception:
        pass


def _confirm_apply(yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("确认把新增 host 行写入 topology.yaml（同名行不覆盖）? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# 周期重扫（A2）：复用 cron 调度器（runbook_schedule.py 同款注册模式）
# ---------------------------------------------------------------------------


def _rescan_script_path(home: Path) -> Path:
    return home / "runtime" / "topo_rescan.sh"


def _write_rescan_script(home: Path) -> Path:
    """生成 cron 用的重扫脚本（只读报告路径，绝不写拓扑）。"""
    path = _rescan_script_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    python = sys.executable or "python3"
    home_str = str(home).replace('"', '\\"')
    path.write_text(
        "#!/bin/sh\n"
        "# vigil topo-sync 周期重扫（cron no_agent job 用；漂移报告只读，不写拓扑）。\n"
        f'VIGIL_HOME="{home_str}"\n'
        "export VIGIL_HOME\n"
        f'exec "{python}" -m hermes_cli.topo_sync\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o700)
    return path


def _existing_topo_sync_job():
    from cron.jobs import list_jobs
    for job in list_jobs(include_disabled=True):
        if str(job.get("name") or "") == _TOPO_SYNC_JOB_NAME:
            return job
    return None


def register_topo_rescan_schedule(interval: str,
                                  home: Optional[Path] = None) -> str:
    """注册/更新/注销周期重扫 job（幂等；复用 cron 调度器，无新守护进程）。

    interval 为空字符串/"off" → 注销。返回 "registered" | "updated" |
    "unregistered" | "noop"。
    """
    from cron.jobs import create_job, list_jobs, remove_job, update_job, use_cron_store

    home = Path(home or _active_home()).resolve()
    interval = str(interval or "").strip()
    with use_cron_store(home):
        existing = None
        for job in list_jobs(include_disabled=True):
            if str(job.get("name") or "") == _TOPO_SYNC_JOB_NAME:
                existing = job
                break
        if not interval or interval.lower() == "off":
            if existing:
                remove_job(str(existing["id"]))
                print(f"· 已注销周期重扫（cron job {existing['id']}）。")
                return "unregistered"
            print("· 未注册周期重扫，无需注销。")
            return "noop"
        script = _write_rescan_script(home)
        if existing:
            update_job(str(existing["id"]), {
                "schedule": interval,
                "schedule_display": interval,
                "script": str(script),
                "no_agent": True,
            })
            print(f"· 周期重扫已更新：{interval}（cron job {existing['id']}，"
                  f"脚本 {script}）。")
            return "updated"
        job = create_job(
            prompt=None,
            schedule=interval,
            name=_TOPO_SYNC_JOB_NAME,
            script=str(script),
            no_agent=True,
            deliver="local",
        )
        print(f"· 周期重扫已注册：{interval}（cron job {job['id']}，"
              f"脚本 {script}；漂移报告只读，不写拓扑）。")
        return "registered"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vigil topo-sync",
        description=(
            "拓扑自维护：terraform.tfstate 同步源 + 漂移报告（默认只报告不落盘）"
            "+ 周期重扫（复用 cron 调度器，opt-in 默认关）。"
        ),
    )
    parser.add_argument(
        "--tfstate", action="append", default=None,
        help="tfstate/terraform show -json JSON 文件路径（可重复；缺省读 "
             "config ops.topology.tfstate_paths）",
    )
    parser.add_argument(
        "--env", default=None,
        help="tfstate 无 env tag 时的缺省环境（tag > 本参数 > local）",
    )
    parser.add_argument("--cluster", default=None, help="新 host 行的 cluster 标记（可选）")
    parser.add_argument(
        "--apply", action="store_true",
        help="把新增 host 行写入 topology.yaml（同名既有行绝不覆盖；缺省只报告）",
    )
    parser.add_argument("--yes", action="store_true", help="跳过 --apply 的交互确认")
    parser.add_argument(
        "--schedule", default=None,
        help="注册周期重扫 cron job（如 24h/1d；off = 注销）。opt-in：未注册 = 不重扫",
    )
    args = parser.parse_args(argv)

    home = _active_home()

    # --schedule 分支：只管调度注册（报告/应用与本分支互斥，保持命令语义单一）。
    if args.schedule is not None:
        register_topo_rescan_schedule(args.schedule, home)
        return 0

    tfstate_paths = _resolve_tfstate_paths(args)
    if not tfstate_paths:
        print("✗ 未指定 --tfstate，config ops.topology.tfstate_paths 也为空。\n"
              "  用法：vigil topo-sync --tfstate <path> [--env <env>] [--apply]",
              file=sys.stderr)
        return 2

    try:
        rows = _read_rows(tfstate_paths, (args.env or "").strip(),
                          (args.cluster or "").strip())
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("✗ tfstate 中没有可映射的云主机资源（alicloud/aws/azurerm 实例族）。",
              file=sys.stderr)
        return 1

    from tools.topo_discovery import (
        apply_tfstate_rows,
        topo_drift_report,
        write_drift_report,
    )

    report = topo_drift_report(home, rows)
    _print_report(report)
    report_path = write_drift_report(home, report)
    print(f"· 报告已写入：{report_path}")
    _record_drift_event(report)

    if not args.apply:
        print("\n· 默认只报告不落盘。确认无误后加 --apply 写入新增行"
              "（同名既有行不覆盖；消失行不自动删除）。")
        return 0

    if not report["added"]:
        print("\n· 无新增行，无需落盘。")
        return 0
    if not _confirm_apply(args.yes):
        print("\n· 已取消，未写入。")
        return 0

    result = apply_tfstate_rows(home, report["added"])
    print(f"\n· 已写入 {len(result['applied'])} 行（needs_review=true，"
          "请 topo_update 确认后进入权威拓扑）：")
    for name in result["applied"]:
        print(f"    + {name}")
    if result["skipped"]:
        print(f"· 跳过同名既有行 {len(result['skipped'])} 个（绝不覆盖）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
