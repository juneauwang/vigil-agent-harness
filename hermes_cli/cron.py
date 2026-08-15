"""
Cron subcommand for vigil CLI.

Handles standalone cron management commands like list, create, edit,
pause/resume/run/remove, status, and tick.
"""

import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.colors import Colors, color

# Gateway-lifecycle command detection lives in ``cron.lifecycle_guard`` so it
# can be shared across every job-creation path (CLI + the agent's ``cronjob``
# model tool via ``cron.jobs.create_job``) without a circular import. Re-export
# ``_contains_gateway_lifecycle_command`` here for back-compat: ``tools/
# terminal_tool.py`` imports it from this module to hard-block the same
# commands at execution time when ``_VIGIL_GATEWAY=1``.
from cron.lifecycle_guard import (  # noqa: F401  (re-exported for terminal_tool)
    contains_gateway_lifecycle_command as _contains_gateway_lifecycle_command,
)


def _normalize_skills(single_skill=None, skills: Optional[Iterable[str]] = None) -> Optional[List[str]]:
    if skills is None:
        if single_skill is None:
            return None
        raw_items = [single_skill]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _cron_api(**kwargs):
    from tools.cronjob_tools import cronjob as cronjob_tool

    return json.loads(cronjob_tool(**kwargs))


def _active_cron_provider_name() -> str:
    """Name of the resolved cron scheduler provider ('builtin', 'chronos', …).

    Best-effort + offline (``resolve_cron_scheduler`` reads config and the
    provider's ``is_available()`` contract forbids network). Returns 'builtin'
    on any failure so callers fall back to the historical ticker-based checks.
    """
    try:
        from cron.scheduler_provider import resolve_cron_scheduler

        return resolve_cron_scheduler().name or "builtin"
    except Exception:
        return "builtin"


def _read_process_environ(pid: int) -> Optional[str]:
    """Read ``/proc/<pid>/environ`` (NUL-separated), or None when unavailable.

    systemd unit 的 ``Environment=VIGIL_HOME=...`` 会出现在进程环境里——
    是区分安装归属（Vigil vs upstream Vigil）最可靠的信号。
    """
    path = Path(f"/proc/{pid}/environ")
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _pid_belongs_to_home(pid: int, home: Path) -> bool:
    """True when the PID is THIS installation's (``home``) gateway process.

    优先级（OPS-DELTA #12 缺陷 4/5 的归属判定）：
      1. ``/proc/<pid>/environ`` 的 ``VIGIL_HOME=<home>`` 精确匹配——最可靠；
      2. 环境读不到时回退命令行 ``--profile`` / ``VIGIL_HOME=`` 匹配
         （``gateway.status._command_line_belongs_to_profile``）；
      3. 两者都读不到（权限/平台）→ 保留（无法判定时宁可多报，不误杀）。
    """
    env = _read_process_environ(pid)
    if env is not None:
        home_value = None
        for line in env.split("\x00"):
            if line.startswith("VIGIL_HOME="):
                home_value = line[len("VIGIL_HOME="):]
                break
        if home_value:
            try:
                return Path(home_value).expanduser().resolve() == home
            except Exception:
                return False
    try:
        from gateway.status import (
            _command_line_belongs_to_profile,
            _read_process_cmdline,
        )
        cmdline = _read_process_cmdline(pid)
        if cmdline:
            return _command_line_belongs_to_profile(cmdline, home)
    except Exception:
        pass
    return True


def _vigil_owned_gateway_pids() -> List[int]:
    """Gateway PIDs belonging to THIS installation (filtered by VIGIL_HOME).

    ``hermes_cli.gateway.find_gateway_pids()`` 的 systemd 服务扫描
    （``hermes-gateway*`` 单位）不区分安装归属——upstream Vigil 的
    ``hermes-gateway.service`` 会被当成自己的。这里逐 PID 按 VIGIL_HOME /
    --profile 过滤：upstream 一律不算，cron status 不再把别人的常驻进程当
    自己的调度载体（假健康）。
    """
    from hermes_cli.gateway import find_gateway_pids
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home()).resolve()
    return [pid for pid in find_gateway_pids() if _pid_belongs_to_home(pid, home)]


def _warn_if_gateway_not_running() -> None:
    """Warn that scheduled jobs won't fire unless the gateway is running.

    The cron ticker only runs inside the gateway (``_start_cron_ticker`` in
    gateway/run.py); there is no standalone cron daemon. Without a running
    gateway, ``next_run_at`` passes but jobs never fire and ``last_run_at``
    stays null — the most common cron support report (#51038). Surfacing this
    at create/list time, when the user is right there, prevents it.

    An external provider (e.g. Chronos) fires jobs via a NAS-mediated webhook,
    NOT the in-process ticker, so a momentarily-absent gateway process does not
    mean jobs won't fire — the warning would be a false alarm. Stay quiet for
    any non-builtin provider; the gateway-process heuristic only speaks to the
    built-in ticker's trigger.
    """
    try:
        if _active_cron_provider_name() != "builtin":
            return

        if _vigil_owned_gateway_pids():
            return
    except Exception:
        # If we can't determine gateway state, stay quiet rather than nag.
        return

    print(color("  ⚠  Gateway is not running — jobs won't fire automatically.", Colors.YELLOW))
    print(color("     Start it with: vigil gateway install", Colors.DIM))
    print(color("                    sudo vigil gateway install --system  # Linux servers", Colors.DIM))
    print(color("     Check status:  vigil cron status", Colors.DIM))


def cron_list(show_all: bool = False):
    """List all scheduled jobs."""
    from cron.jobs import list_jobs

    jobs = list_jobs(include_disabled=show_all)

    if not jobs:
        print(color("No scheduled jobs.", Colors.DIM))
        print(color("Create one with 'vigil cron create ...' or the /cron command in chat.", Colors.DIM))
        return

    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                         Scheduled Jobs                                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()

    for job in jobs:
        job_id = job.get("id", "?")
        name = job.get("name", "(unnamed)")
        schedule = job.get("schedule_display", job.get("schedule", {}).get("value", "?"))
        state = job.get("state", "scheduled" if job.get("enabled", True) else "paused")
        next_run = job.get("next_run_at", "?")

        # `repeat` may be present-but-null in the job record (e.g. a one-shot
        # job persisted with "repeat": null), so coalesce to {} rather than
        # relying on the dict-default, which only applies to a missing key.
        repeat_info = job.get("repeat") or {}
        repeat_times = repeat_info.get("times")
        repeat_completed = repeat_info.get("completed", 0)
        repeat_str = f"{repeat_completed}/{repeat_times}" if repeat_times else "∞"

        # `deliver` may be present-but-null in the job record (same pitfall as
        # `repeat` above), so coalesce to the default rather than relying on the
        # dict-default, which only applies to a missing key. A null value would
        # otherwise reach `", ".join(None)` and crash the whole listing (#32896).
        deliver = job.get("deliver") or ["local"]
        if isinstance(deliver, str):
            deliver = [deliver]
        deliver_str = ", ".join(deliver)

        skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
        if state == "paused":
            status = color("[paused]", Colors.YELLOW)
        elif state == "completed":
            status = color("[completed]", Colors.BLUE)
        elif job.get("enabled", True):
            status = color("[active]", Colors.GREEN)
        else:
            status = color("[disabled]", Colors.RED)

        print(f"  {color(job_id, Colors.YELLOW)} {status}")
        print(f"    Name:      {name}")
        print(f"    Schedule:  {schedule}")
        print(f"    Repeat:    {repeat_str}")
        print(f"    Next run:  {next_run}")
        print(f"    Deliver:   {deliver_str}")
        if skills:
            print(f"    Skills:    {', '.join(skills)}")
        script = job.get("script")
        if script:
            print(f"    Script:    {script}")
        if job.get("no_agent"):
            print(f"    Mode:      {color('no-agent', Colors.DIM)} (script stdout delivered directly)")
        workdir = job.get("workdir")
        if workdir:
            print(f"    Workdir:   {workdir}")

        # Execution history
        last_status = job.get("last_status")
        if last_status:
            last_run = job.get("last_run_at", "?")
            if last_status == "ok":
                status_display = color("ok", Colors.GREEN)
            else:
                status_display = color(f"{last_status}: {job.get('last_error', '?')}", Colors.RED)
            print(f"    Last run:  {last_run}  {status_display}")

        latest_execution = job.get("latest_execution")
        if latest_execution:
            print(
                f"    Execution: {latest_execution.get('status', '?')}  "
                f"{latest_execution.get('id', '?')}"
            )

        delivery_err = job.get("last_delivery_error")
        if delivery_err:
            print(f"    {color('⚠ Delivery failed:', Colors.YELLOW)} {delivery_err}")

        print()

    _warn_if_gateway_not_running()


def cron_tick():
    """Run due jobs once and exit."""
    from cron.scheduler import tick
    tick(verbose=True)


def cron_runs(job_id: Optional[str] = None, limit: int = 20):
    """Show indexed durable cron execution history."""
    from cron.executions import list_executions

    records = list_executions(job_id=job_id, limit=limit)
    if not records:
        print("No cron execution attempts recorded.")
        return
    for record in records:
        print(
            f"{record.get('id', '?')}  {record.get('status', '?'):<9}  "
            f"job={record.get('job_id', '?')}  source={record.get('source', '?')}  "
            f"{record.get('claimed_at', '?')}"
        )
        if record.get("error"):
            print(f"    {record['error']}")


def cron_status():
    """Show cron execution status."""
    from cron.jobs import list_jobs

    print()

    provider = _active_cron_provider_name()
    if provider != "builtin":
        # An external provider (e.g. Chronos) does NOT run the in-process 60s
        # ticker — it arms one external one-shot per job and is fired by a
        # NAS-mediated webhook, so between fires there is intentionally NO
        # ticker thread and NO heartbeat file. Reporting the ticker-heartbeat
        # staleness here would always say "stalled / not firing" on a perfectly
        # healthy Chronos instance. Report the provider instead and skip the
        # ticker-liveness heuristics entirely.
        print(color(
            f"✓ Cron provider: {provider} — jobs fire via the managed scheduler, "
            "not the in-process ticker.",
            Colors.GREEN,
        ))
        print(color(
            "  (No ticker heartbeat is expected for an external provider; "
            "due jobs are delivered by an authenticated webhook.)",
            Colors.DIM,
        ))
        print()
        _print_active_jobs_summary(list_jobs(include_disabled=False))
        print()
        return

    pids = _vigil_owned_gateway_pids()
    # Gateway 进程存活 ≠ 调度健康：ticker 线程可能已死或每轮失败（#32612,
    # #32895），且 gateway PID 必须属于本安装（#12 缺陷 4/5——upstream agent harness
    # 的 gateway 一律不算）。Gateway 状态与 Ticker 状态分开显示。
    from cron.jobs import (
        get_ticker_heartbeat_age,
        get_ticker_last_error,
        get_ticker_success_age,
        TICKER_INTERVAL_SECONDS,
    )

    # 心跳新鲜阈值：< 2×tick 间隔视为活跃（任务契约）；明显死亡/失败沿用
    # 既有的 ~3 个 tick 间隔 + 余量阈值。
    FRESH_AFTER = TICKER_INTERVAL_SECONDS * 2
    STALE_AFTER = TICKER_INTERVAL_SECONDS * 3 + 20  # = 200s at the 60s default
    hb_age = get_ticker_heartbeat_age()
    ok_age = get_ticker_success_age()

    # Gateway 行（安装归属已过滤）
    if pids:
        print(color(f"  Gateway: ✓ 运行中 (PID {', '.join(map(str, pids))})", Colors.GREEN))
    else:
        print(color("  Gateway: ✗ 未运行（无 Vigil 归属的 gateway 进程）", Colors.RED))

    # Ticker 行（真实心跳状态）
    if hb_age is not None and hb_age < FRESH_AFTER:
        if ok_age is not None and ok_age > STALE_AFTER:
            # 心跳新鲜但 long 时间没有成功 tick → 每轮失败。
            print(color(
                "  Ticker: ⚠ 心跳新鲜但近期无成功 tick——可能每轮失败。",
                Colors.YELLOW,
            ))
            last_error = get_ticker_last_error()
            if last_error:
                # Show WHY ticks fail — e.g. a root-rewritten jobs.json
                # (PermissionError) that silently locked out the ticker's
                # uid for ~14h in the field (#68483).
                print(color(f"  Last tick error: {last_error}", Colors.RED))
                if "Permission denied" in last_error:
                    print(color(
                        "  Hint: jobs.json may be owned by another user "
                        "(e.g. rewritten by a root `docker exec vigil "
                        "vigil cron ...`). Fix ownership to match the "
                        "gateway user, and prefer `docker exec -u <uid>:<gid>`.",
                        Colors.YELLOW,
                    ))
            print("  Check the gateway log for 'Cron tick error'.")
        else:
            print(color("  Ticker: ✓ ticker 活跃，调度正常", Colors.GREEN))
    else:
        age_note = (
            f"（心跳 {int(hb_age)}s 前过期，间隔 ~{TICKER_INTERVAL_SECONDS}s）"
            if hb_age is not None
            else "（心跳缺失）"
        )
        print(color(
            f"  Ticker: ⚠ ticker 未运行{age_note}——会话未打开或 vigil-watch 未安装",
            Colors.YELLOW,
        ))
        print(color(
            "    调度依赖会话进程内的 ticker；常驻采集见 `vigil watch install`。",
            Colors.DIM,
        ))

    # 采集常驻接管提示（best-effort；watch 未安装/不可判定时静默）。
    try:
        from hermes_cli.watch import watch_service_active
        if watch_service_active() is True:
            print(color(
                "  （采集已由 vigil-watch 常驻接管；巡检/告警采集状态以 "
                "`vigil watch status` 为准。）",
                Colors.DIM,
            ))
    except Exception:
        pass

    if not pids:
        print()
        print("  To enable automatic execution:")
        print("    vigil gateway install    # Install as a user service")
        print("    sudo vigil gateway install --system  # Linux servers: boot-time system service")
        print("    vigil gateway            # Or run in foreground")

    print()

    _print_active_jobs_summary(list_jobs(include_disabled=False))

    print()


def _print_active_jobs_summary(jobs) -> None:
    """Print the '<N> active job(s)' + next-run line shared by every status
    path (built-in ticker AND external provider)."""
    if jobs:
        next_runs = [j.get("next_run_at") for j in jobs if j.get("next_run_at")]
        print(f"  {len(jobs)} active job(s)")
        if next_runs:
            print(f"  Next run: {min(next_runs)}")
    else:
        print("  No active jobs")


def cron_create(args):
    # The gateway-lifecycle guard lives in cron.jobs.create_job so it fires on
    # every job-creation path (this CLI subcommand AND the agent's `cronjob`
    # model tool, which calls create_job directly). When it blocks, create_job
    # raises GatewayLifecycleBlocked, the `cronjob` tool wrapper catches it and
    # returns it as result["error"], and the `if not result.get("success")`
    # branch below prints it in red and exits 1 — same UX as before.
    result = _cron_api(
        action="create",
        schedule=args.schedule,
        prompt=args.prompt,
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skill=getattr(args, "skill", None),
        skills=_normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None)),
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        model=getattr(args, "model", None),
        provider=getattr(args, "model_provider", None),
        no_agent=getattr(args, "no_agent", False) or None,
    )
    if not result.get("success"):
        print(color(f"Failed to create job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    print(color(f"Created job: {result['job_id']}", Colors.GREEN))
    print(f"  Name: {result['name']}")
    print(f"  Schedule: {result['schedule']}")
    if result.get("skills"):
        print(f"  Skills: {', '.join(result['skills'])}")
    job_data = result.get("job", {})
    if job_data.get("script"):
        print(f"  Script: {job_data['script']}")
    if job_data.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if job_data.get("workdir"):
        print(f"  Workdir: {job_data['workdir']}")
    print(f"  Next run: {result['next_run_at']}")
    _warn_if_gateway_not_running()
    return 0


def cron_edit(args):
    from cron.jobs import AmbiguousJobReference, resolve_job_ref

    try:
        job = resolve_job_ref(args.job_id)
    except AmbiguousJobReference as exc:
        print(color(str(exc), Colors.RED))
        for m in exc.matches:
            print(f"  {m['id']}  (name: {m.get('name')!r})")
        return 1
    if not job:
        print(color(f"Job not found: {args.job_id}", Colors.RED))
        return 1

    existing_skills = list(job.get("skills") or ([] if not job.get("skill") else [job.get("skill")]))
    replacement_skills = _normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None))
    add_skills = _normalize_skills(None, getattr(args, "add_skills", None)) or []
    remove_skills = set(_normalize_skills(None, getattr(args, "remove_skills", None)) or [])

    final_skills = None
    if getattr(args, "clear_skills", False):
        final_skills = []
    elif replacement_skills is not None:
        final_skills = replacement_skills
    elif add_skills or remove_skills:
        final_skills = [skill for skill in existing_skills if skill not in remove_skills]
        for skill in add_skills:
            if skill not in final_skills:
                final_skills.append(skill)

    result = _cron_api(
        action="update",
        job_id=args.job_id,
        schedule=getattr(args, "schedule", None),
        prompt=getattr(args, "prompt", None),
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skills=final_skills,
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        model=getattr(args, "model", None),
        provider=getattr(args, "model_provider", None),
        no_agent=getattr(args, "no_agent", None),
    )
    if not result.get("success"):
        print(color(f"Failed to update job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1

    updated = result["job"]
    print(color(f"Updated job: {updated['job_id']}", Colors.GREEN))
    print(f"  Name: {updated['name']}")
    print(f"  Schedule: {updated['schedule']}")
    if updated.get("skills"):
        print(f"  Skills: {', '.join(updated['skills'])}")
    else:
        print("  Skills: none")
    if updated.get("script"):
        print(f"  Script: {updated['script']}")
    if updated.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if updated.get("workdir"):
        print(f"  Workdir: {updated['workdir']}")
    return 0


def _job_action(action: str, job_id: str, success_verb: str) -> int:
    result = _cron_api(action=action, job_id=job_id)
    if not result.get("success"):
        print(color(f"Failed to {action} job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    job = result.get("job") or result.get("removed_job") or {}
    print(color(f"{success_verb} job: {job.get('name', job_id)} ({job_id})", Colors.GREEN))
    if action in {"resume", "run"} and result.get("job", {}).get("next_run_at"):
        print(f"  Next run: {result['job']['next_run_at']}")
    if action == "run":
        job = result.get("job", {})
        if job.get("executed"):
            outcome = "succeeded" if job.get("execution_success") else "failed"
            print(f"  Ran now: {outcome}.")
        elif job.get("execution_skipped"):
            print(f"  {job['execution_skipped']}")
        else:
            print("  It will run on the next scheduler tick.")
    return 0


def cron_command(args):
    """Handle cron subcommands."""
    subcmd = getattr(args, 'cron_command', None)

    if subcmd is None or subcmd == "list":
        show_all = getattr(args, 'all', False)
        cron_list(show_all)
        return 0

    if subcmd == "status":
        cron_status()
        return 0

    if subcmd == "tick":
        cron_tick()
        return 0

    if subcmd in {"runs", "history"}:
        cron_runs(getattr(args, "job_id", None), getattr(args, "limit", 20))
        return 0

    if subcmd in {"create", "add"}:
        return cron_create(args)

    if subcmd == "edit":
        return cron_edit(args)

    if subcmd == "pause":
        return _job_action("pause", args.job_id, "Paused")

    if subcmd == "resume":
        return _job_action("resume", args.job_id, "Resumed")

    if subcmd == "run":
        return _job_action("run", args.job_id, "Triggered")

    if subcmd in {"remove", "rm", "delete"}:
        return _job_action("remove", args.job_id, "Removed")

    print(f"Unknown cron command: {subcmd}")
    print("Usage: vigil cron [list|create|edit|pause|resume|run|remove|status|runs|tick]")
    sys.exit(1)
