"""YAPL P4 runbook 调度注册（yapl-design.md §10.7 schedule + §11.4 执行豁免）。

runbook 带 ``schedule: {cron, timezone}`` 时自动注册到现有 cron 调度器（不新造
轮子）：job 记录带 ``runbook: <name>`` 标记，``cron/scheduler.run_job`` 走
runbook 分支（确定性执行引擎，无 LLM）。注册/更新/注销幂等（按 runbook 标记
查找）。

执行豁免（§11.4）在引擎侧（``runbook_exec.execute_runbook(scheduled=True)``）：
预审标记存在 + 内容哈希未漂移 → 跳过逐次审批 + 执行后记录 + 通知（事后审计）；
未预审 → 拒绝并提示先过资产审批。

接线点（OPS-DELTA 注明）：cron job 的 schedule 由 ``cron.jobs.parse_schedule``
解析（croniter），ticker 按 hermes 配置时区推进；runbook.schedule.timezone 是
权威墙钟语义（校验 + 展示 + 执行记录），调度推进时区差异登记在案（hermes tz
≠ runbook tz 时以 hermes 配置时区为准，OPS-DELTA #73）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _existing_runbook_job(runbook: str, home: Optional[Path]):
    from cron.jobs import list_jobs
    for job in list_jobs(include_disabled=True):
        if str(job.get("runbook") or "") == runbook:
            return job
    return None


def register_runbook_schedule(runbook: str, schedule: Any,
                              home: Optional[Path] = None) -> str:
    """runbook 创建/更新/删除后同步 cron 注册（幂等）。

    Args:
        runbook: runbook 名（kebab-case）。
        schedule: runbook 的 schedule（{cron, timezone}）；None/空 = 注销。
        home: VIGIL_HOME（测试注入；走 ``use_cron_store`` 上下文路由）。

    Returns:
        "registered" | "updated" | "unregistered" | "noop"（无 schedule 且
        无既有 job）。
    """
    from cron.jobs import create_job, remove_job, update_job, use_cron_store

    runbook = str(runbook or "").strip()
    home = Path(home).resolve() if home else None
    has_schedule = isinstance(schedule, dict) and bool(
        str(schedule.get("cron") or "").strip())

    with use_cron_store(home):
        existing = _existing_runbook_job(runbook, home)
        if not has_schedule:
            if existing:
                remove_job(str(existing["id"]))
                logger.info("runbook 调度注销: %s（job %s）", runbook, existing["id"])
                return "unregistered"
            return "noop"
        cron = str(schedule.get("cron") or "").strip()
        tz = str(schedule.get("timezone") or "").strip()
        if existing:
            update_job(str(existing["id"]), {
                "schedule": cron,
                "schedule_display": cron,
                "runbook_timezone": tz,
            })
            logger.info("runbook 调度更新: %s → %s（tz=%s）", runbook, cron, tz)
            return "updated"
        job = create_job(
            prompt=None,
            schedule=cron,
            name=f"runbook:{runbook}",
            runbook=runbook,
            deliver="local",
        )
        update_job(str(job["id"]), {"runbook_timezone": tz})
        logger.info("runbook 调度注册: %s → %s（tz=%s, job %s）",
                    runbook, cron, tz, job["id"])
        return "registered"


def runbook_schedule_status(runbook: str, home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """查询 runbook 的调度 job（只读；无则 None）。"""
    home = Path(home).resolve() if home else None
    from cron.jobs import use_cron_store
    with use_cron_store(home):
        job = _existing_runbook_job(runbook, home)
        if job is None:
            return None
        return {
            "job_id": job.get("id"),
            "schedule": job.get("schedule_display") or job.get("schedule"),
            "timezone": job.get("runbook_timezone") or "",
            "enabled": job.get("enabled", True),
            "next_run_at": job.get("next_run_at"),
        }
