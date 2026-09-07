"""``vigil alerts auto-dispatch`` —— 告警驱动的 runbook 自动派发（任务25）。

产品承诺（yapl-design.md 头部）："自动值守 = 有 runbook 走预审 SOP，无 runbook
通知人介入"。本模块补上最后一环：活跃告警 → 匹配器命中 **且** runbook 作者
显式声明 ``alert_auto_run: true``（authoring-time 人工授权，随资产审批落盘）
→ 经 **与定时执行完全相同** 的豁免通道执行（``execute_runbook(scheduled=True)``，
预审标记 + 内容哈希校验在引擎侧 fail-closed），事后审计 + 结果摘要。

安全模型（与 runbook_schedule/topo_sync 同款，不新造）：
- **无新守护进程、无 LLM**：复用 cron 调度器，no_agent 脚本即本模块的
  ``python -m hermes_cli.alert_autodispatch``（0700，VIGIL_HOME 固定）；
- **opt-in 默认关**：``ops.alerts.auto_dispatch.enabled``（默认 false）+
  cron 注册（``--schedule``）；脚本每次 tick 重读门——配置关掉即使 job 残留
  也不派发；
- **幂等**：``alertname|instance|startsAt`` 键一次 occurrence 只派发一次
  （startsAt 在告警恢复后重燃时重置 = 新 occurrence，对齐 watch_collect
  的 alertname|instance 去重约定）；另有单 tick 派发上限（防风暴）；
- **无授权 → 什么都不执行**：未授权 runbook 命中维持 batch87 建议闭环；
- **不削弱 force_manual fail-closed**：派发只走 scheduled 豁免通道，逐步骤
  人工门在豁免下由 authoring-time 资产审批替代（runbook_exec 定时语义）；
  预审缺失/哈希漂移 → 引擎拒绝 → 审计标 needs-human，降级回建议闭环。

数据：状态 ``runtime/alert_autodispatch_state.json``；审计
``runtime/alert_autodispatch.jsonl``（追加式，best-effort 不阻断）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_JOB_NAME = "alert_autodispatch"
_STATE_DIRNAME = "runtime"
_STATE_FILENAME = "alert_autodispatch_state.json"
_AUDIT_FILENAME = "alert_autodispatch.jsonl"
_MAX_STATE_ENTRIES = 500
_MAX_AUDIT_LINES = 500
_DEFAULT_MAX_PER_TICK = 3

_state_lock = threading.Lock()
_audit_lock = threading.Lock()

_STATUS_OK = 0
_STATUS_ERROR = 1


def _active_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).resolve()


def auto_dispatch_config(home: Optional[Path] = None) -> Dict[str, Any]:
    """读 ``ops.alerts.auto_dispatch`` 配置（enabled/interval/max_per_tick）。"""
    del home  # 配置是 profile 感知的（load_config_readonly 走当前 VIGIL_HOME）
    try:
        from hermes_cli.config import load_config_readonly
        ops = load_config_readonly().get("ops") or {}
        alerts = ops.get("alerts") or {}
        cfg = alerts.get("auto_dispatch") or {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        max_per_tick = max(1, int(cfg.get("max_per_tick") or _DEFAULT_MAX_PER_TICK))
    except (TypeError, ValueError):
        max_per_tick = _DEFAULT_MAX_PER_TICK
    return {
        "enabled": cfg.get("enabled") is True,
        "interval": str(cfg.get("interval") or "").strip(),
        "max_per_tick": max_per_tick,
    }


def auto_dispatch_enabled() -> bool:
    """门（opt-in 默认关）：``ops.alerts.auto_dispatch.enabled is True``。"""
    return auto_dispatch_config()["enabled"]


# ---------------------------------------------------------------------------
# 幂等状态（runtime/alert_autodispatch_state.json）
# ---------------------------------------------------------------------------

def _state_path(home: Path) -> Path:
    return home / _STATE_DIRNAME / _STATE_FILENAME


def _occurrence_key(alert: Dict[str, Any]) -> str:
    """告警 occurrence 键：alertname|instance|startsAt。

    startsAt 在告警恢复后重新点燃时由 Alertmanager 重置 → 恢复后重燃 =
    新 occurrence，可再次派发；活跃期间同键不重复派发。
    """
    return "|".join(str(alert.get(k) or "-") for k in
                    ("alertname", "instance", "startsAt"))


def _load_state(home: Path) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(home).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(home: Path, state: Dict[str, Any]) -> None:
    path = _state_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 容量截断：只留最近 _MAX_STATE_ENTRIES 条 occurrence 记录
        items = sorted(state.items(), key=lambda kv: str(kv[1].get("ts") or ""))
        trimmed = dict(items[-_MAX_STATE_ENTRIES:])
        path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception as exc:
        logger.warning("alert_autodispatch 状态写入失败: %s", exc)


# ---------------------------------------------------------------------------
# 审计（runtime/alert_autodispatch.jsonl）
# ---------------------------------------------------------------------------

def audit_path(home: Optional[Path] = None) -> Path:
    home = Path(home) if home is not None else _active_home()
    return home / _STATE_DIRNAME / _AUDIT_FILENAME


def record_dispatch_audit(home: Optional[Path], entry: Dict[str, Any]) -> None:
    """追加一条派发审计（JSONL）。best-effort：失败只记日志，不阻断。"""
    path = audit_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _audit_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > _MAX_AUDIT_LINES:
                path.write_text("\n".join(lines[-_MAX_AUDIT_LINES:]) + "\n",
                                encoding="utf-8")
    except Exception as exc:
        logger.warning("alert_autodispatch 审计写入失败: %s", exc)


def recent_dispatch_audit(home: Optional[Path] = None,
                          limit: int = 50) -> List[Dict[str, Any]]:
    """最近 N 条派发审计（新→旧），供回看。"""
    path = audit_path(home)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("alert_autodispatch 审计读取失败: %s", exc)
        return []
    rows.reverse()
    return rows[: max(1, min(int(limit) if limit else 50, 200))]


# ---------------------------------------------------------------------------
# 授权 runbook 枚举（alert_auto_run: true + triggers 非空）
# ---------------------------------------------------------------------------

def authorized_runbooks(home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """返回 {runbook_name: data}：显式授权（alert_auto_run true）的 runbook。

    加载复用 alert_runbook 的容错枚举（坏文件跳过不炸批）。校验器保证
    alert_auto_run true 必有 triggers；历史文件手工改坏的在此再兜一层。
    """
    from tools.alert_runbook import _load_valid_runbooks
    out: Dict[str, Dict[str, Any]] = {}
    for data in _load_valid_runbooks(home):
        if data.get("alert_auto_run") is True and (data.get("triggers") or []):
            out[str(data.get("name") or "")] = data
    return out


# ---------------------------------------------------------------------------
# 单次派发 tick
# ---------------------------------------------------------------------------

_OK_RESULTS = ("ok", "rolled_back")


def run_once(home: Optional[Path] = None, *,
             fetch_alerts: Optional[Callable[[], List[Dict[str, Any]]]] = None,
             execute: Optional[Callable[..., Dict[str, Any]]] = None,
             now: Optional[Callable[[], float]] = None) -> str:
    """一次派发 tick（cron no_agent 脚本入口）。返回人类可读摘要（永不抛）。

    流程：门 → 拉活跃告警 → 授权 runbook 枚举 → 逐条匹配 → 授权 + severity
    范围 + 幂等 + 上限 → scheduled 豁免通道执行 → 状态/审计落盘。
    """
    home = Path(home).resolve() if home is not None else _active_home()
    now = now or time.time
    cfg = auto_dispatch_config(home)
    if not cfg["enabled"]:
        return "alert 自动派发未启用（ops.alerts.auto_dispatch.enabled: false），跳过。"

    try:
        if fetch_alerts is not None:
            alerts = fetch_alerts()
        else:
            from hermes_cli.monitoring import fetch_active_alerts
            alerts = fetch_active_alerts().get("alerts") or []
    except Exception as exc:
        # 上游不可用：不派发不审计告警级条目，tick 记录后返回（cron 会把
        # 非零退出转为失败文档——这里保持 0 退出 + 文本说明即可）。
        logger.warning("alert_autodispatch: 活跃告警拉取失败: %s", exc)
        return f"活跃告警拉取失败，本轮跳过：{exc}"

    authorized = authorized_runbooks(home)
    summary_lines: List[str] = []
    dispatched = 0
    skipped_authorized = 0
    new_state_entries: Dict[str, Any] = {}

    if alerts and authorized:
        state = _load_state(home)
        from tools.alert_runbook import match_alert_to_runbook
        for alert in alerts:
            if dispatched >= cfg["max_per_tick"]:
                summary_lines.append("· 达到单 tick 派发上限，其余告警留待下轮。")
                break
            disposition = match_alert_to_runbook(alert, home=home)
            if not disposition.get("matched"):
                continue  # 无匹配 → 通知人介入路径（建议闭环已覆盖）
            rb_name = str(disposition.get("runbook") or "")
            data = authorized.get(rb_name)
            if data is None:
                skipped_authorized += 1
                continue  # 命中但未授权 → 维持建议闭环，绝不执行
            key = _occurrence_key(alert)
            if key in state:
                summary_lines.append(
                    f"· {alert.get('alertname')}（{alert.get('instance')}）："
                    f"本轮已派发过 {rb_name}，不重复派发。")
                continue
            allowed_sev = data.get("alert_auto_severity")
            if isinstance(allowed_sev, list) and allowed_sev and \
                    str(alert.get("severity") or "") not in {str(s) for s in allowed_sev}:
                summary_lines.append(
                    f"· {alert.get('alertname')}：severity {alert.get('severity')} "
                    f"不在 {rb_name} 的 alert_auto_severity 白名单，跳过自动执行。")
                continue
            # 派发：scheduled 豁免通道（预审标记 + 内容哈希校验在引擎内
            # fail-closed；被拒 → blocked → 审计标 needs-human）。
            t0 = now()
            if execute is not None:
                result = execute(data, home=home, scheduled=True,
                                 trigger_context={"trigger": "alert",
                                                  "alert": {k: alert.get(k) for k in
                                                            ("alertname", "instance",
                                                             "severity", "startsAt")},
                                                  "matched_keyword": disposition.get("matched_keyword")})
            else:
                from tools.runbook_exec import execute_runbook
                result = execute_runbook(
                    data, env="", home=home, scheduled=True,
                    trigger_context={
                        "trigger": "alert",
                        "alert": {k: alert.get(k) for k in
                                  ("alertname", "instance", "severity", "startsAt")},
                        "matched_keyword": disposition.get("matched_keyword"),
                    },
                )
            status = str(result.get("result") or "unknown")
            needs_human = status not in _OK_RESULTS
            dispatched += 1
            entry = {
                "alertname": alert.get("alertname") or "",
                "severity": alert.get("severity") or "",
                "instance": alert.get("instance") or "",
                "startsAt": alert.get("startsAt") or "",
                "runbook": rb_name,
                "matched_keyword": disposition.get("matched_keyword"),
                "result": status,
                "needs_human": needs_human,
                "error": str(result.get("error") or "")[:400],
                "duration_s": round(now() - t0, 2),
            }
            new_state_entries[key] = {
                "runbook": rb_name,
                "result": status,
                "needs_human": needs_human,
                "ts": _iso_now(),
            }
            record_dispatch_audit(home, {
                "ts": _iso_now(),
                "type": "alert_autodispatch",
                **entry,
            })
            tag = "已自动执行" if not needs_human else "执行未通过（needs-human，已降级建议闭环）"
            summary_lines.append(
                f"· {alert.get('alertname')} → {rb_name}：{status}（{tag}）")

        # 状态合并落盘（含 blocked/failed：occurrence 已消耗，不重试风暴；
        # 告警仍活跃 → 建议/triage 视图照常可见 = 人始终兜底）。
        if new_state_entries:
            state = _load_state(home)
            state.update(new_state_entries)
            _save_state(home, state)

    head = (f"alert 自动派发：活跃告警 {len(alerts)} 条，授权 runbook "
            f"{len(authorized)} 个，本轮派发 {dispatched} 次"
            + (f"，未授权命中 {skipped_authorized} 条（维持建议）" if skipped_authorized else "")
            + "。")
    if summary_lines:
        return head + "\n" + "\n".join(summary_lines)
    return head


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------------------
# cron 注册（task18 topo_sync 同款：no_agent job，0700 脚本，固定 VIGIL_HOME）
# ---------------------------------------------------------------------------

def _script_path(home: Path) -> Path:
    return home / "runtime" / "alert_autodispatch.sh"


def _write_script(home: Path) -> Path:
    """生成 cron 用的派发脚本（每 tick 重读配置门，关了就不派发）。"""
    path = _script_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    python = sys.executable or "python3"
    home_str = str(home).replace('"', '\\"')
    path.write_text(
        "#!/bin/sh\n"
        "# vigil alerts auto-dispatch（cron no_agent job 用；opt-in，配置门在模块内重读）。\n"
        f'VIGIL_HOME="{home_str}"\n'
        "export VIGIL_HOME\n"
        f'exec "{python}" -m hermes_cli.alert_autodispatch\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o700)
    return path


def _existing_job():
    from cron.jobs import list_jobs
    for job in list_jobs(include_disabled=True):
        if str(job.get("name") or "") == _JOB_NAME:
            return job
    return None


def register_alert_autodispatch_schedule(interval: str,
                                         home: Optional[Path] = None) -> str:
    """注册/更新/注销告警自动派发 job（幂等；复用 cron 调度器，无新守护进程）。

    interval 为空字符串/"off" → 注销。返回 "registered" | "updated" |
    "unregistered" | "noop"。
    """
    from cron.jobs import create_job, remove_job, update_job, use_cron_store

    home = Path(home or _active_home()).resolve()
    interval = str(interval or "").strip()
    with use_cron_store(home):
        existing = _existing_job()
        if not interval or interval.lower() == "off":
            if existing:
                remove_job(str(existing["id"]))
                print(f"· 已注销 alert 自动派发（cron job {existing['id']}）。")
                return "unregistered"
            print("· 未注册 alert 自动派发，无需注销。")
            return "noop"
        script = _write_script(home)
        if existing:
            update_job(str(existing["id"]), {
                "schedule": interval,
                "schedule_display": interval,
                "script": str(script),
                "no_agent": True,
            })
            print(f"· alert 自动派发已更新：{interval}（cron job {existing['id']}，"
                  f"脚本 {script}）。")
            return "updated"
        job = create_job(
            prompt=None,
            schedule=interval,
            name=_JOB_NAME,
            script=str(script),
            no_agent=True,
            deliver="local",
        )
        print(f"· alert 自动派发已注册：{interval}（cron job {job['id']}，"
              f"脚本 {script}）。注意：还需 ops.alerts.auto_dispatch.enabled: true "
              "才会实际派发（配置门每次 tick 重读）。")
        return "registered"


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m hermes_cli.alert_autodispatch``：跑一次派发 tick（cron 用）。"""
    try:
        print(run_once(), flush=True)
    except Exception as exc:  # 约定不抛，防御性兜底
        print(f"alert auto-dispatch error: {exc}", flush=True)
        return _STATUS_ERROR
    return _STATUS_OK
