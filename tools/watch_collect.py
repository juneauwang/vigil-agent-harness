"""Vigil 值守采集层（OPS-DELTA #9）：拉模式巡检，落盘 inbox 队列。

采集/分析分层的第一层——**确定性代码，不需要 LLM**，由 systemd --user
常驻服务（``vigil-watch.service`` → ``hermes_cli.watch_collect_loop``）每
5 分钟跑一次；LLM 只做第二层（消费 inbox 分析播报，见 ``tools/watch_tools.py``
的 ``watch_digest``）。session 关闭不影响采集，办公电脑关机也不丢告警
（告警堆在 alertmanager，回来补拉）。

数据契约：:

    ~/.vigil/watch/inbox/<timestamp>.json
    {"collected_at": "<iso>", "alerts": [{alertname, severity, instance, startsAt, state}], "processed": false}

- 有活跃告警 → 写一条 inbox；无告警 → 不写（零成本）；
- 幂等/防重：按 alertname+instance 去重，已在**未处理** inbox 中的同键告警
  不再追加（同告警恢复后再复发 → 旧条目已 processed 或被新状态替换 → 允许
  新条目）；
- 采集失败（网络/配置缺失）→ 只写 ``~/.vigil/watch/errors.log`` 一行，不抛
  异常（systemd 重启循环里不能炸）；网络硬超时 10s（复用 prom_tools）；
- 门控：``ops.watch.enabled: false``（或 ``ops.prometheus.alertmanager`` 未
  配置）→ ``collect_once`` 直接返回不做任何事（硬约束 3）。

只读：本模块只采集落盘，不执行任何命令、不触发任何 agent。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.prom_tools import _prom_config, _resolve_basic_auth, _http_get

logger = logging.getLogger(__name__)

_WATCH_DIRNAME = "watch"
_INBOX_DIRNAME = "inbox"
_ERRORS_FILENAME = "errors.log"
_LAST_COLLECT_FILENAME = "last_collect"


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def watch_dir() -> Path:
    return _hermes_home() / _WATCH_DIRNAME


def inbox_dir() -> Path:
    return watch_dir() / _INBOX_DIRNAME


def errors_log_path() -> Path:
    return watch_dir() / _ERRORS_FILENAME


def last_collect_path() -> Path:
    return watch_dir() / _LAST_COLLECT_FILENAME


def _ops_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}) or {}
    except Exception:
        return {}


def watch_enabled() -> bool:
    """值守采集开关：``ops.watch.enabled: false`` → 关闭（硬约束 3）；缺省开。"""
    ops = _ops_config()
    return ops.get("watch", {}).get("enabled") is not False


def _log_error(message: str) -> None:
    """把一行采集错误追加到 errors.log（带时间戳；写失败静默）。"""
    try:
        errors_log_path().parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}\n"
        with open(errors_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # pragma: no cover - 落盘失败也要保证不抛
        logger.error("watch: failed to write errors.log: %s", exc)


def _fetch_alerts(alertmanager: str, prom_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """拉 Alertmanager /api/v2/alerts，返回紧凑告警摘要（含 vault basic auth）。

    非 200 / 非 JSON / 超时 → 抛异常（由 collect_once 捕获写 errors.log）。
    """
    auth: Optional[str] = None
    try:
        auth = _resolve_basic_auth(prom_cfg)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    resp = _http_get(f"{alertmanager.rstrip('/')}/api/v2/alerts", {}, auth)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Alertmanager HTTP {resp.status_code}: {resp.text[:300]}"
        )
    try:
        raw = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Alertmanager 返回非 JSON: {resp.text[:300]}") from exc
    if not isinstance(raw, list):
        raise RuntimeError("Alertmanager 响应不是告警列表")
    alerts: List[Dict[str, Any]] = []
    for alert in raw:
        if not isinstance(alert, dict):
            continue
        labels = alert.get("labels") or {}
        status = alert.get("status") or {}
        alerts.append({
            "alertname": labels.get("alertname") or "?",
            "severity": labels.get("severity") or "-",
            "instance": labels.get("instance") or "-",
            "startsAt": alert.get("startsAt") or "-",
            "state": status.get("state") or "active",
        })
    return alerts


def _alert_key(alert: Dict[str, Any]) -> str:
    return f"{alert.get('alertname', '?')}|{alert.get('instance', '-')}"


def _unprocessed_keys() -> set:
    """未处理 inbox 条目里的告警键（alertname|instance）集合。"""
    keys: set = set()
    for path in _iter_inbox():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("processed"):
            continue
        for alert in data.get("alerts") or []:
            if isinstance(alert, dict):
                keys.add(_alert_key(alert))
    return keys


def _iter_inbox() -> List[Path]:
    try:
        return sorted(inbox_dir().glob("*.json"))
    except OSError:
        return []


def _write_inbox(alerts: List[Dict[str, Any]], timestamp: str) -> Path:
    """原子写一条 inbox（tmp + rename，防 systemd stop 留下半截文件）。"""
    inbox_dir().mkdir(parents=True, exist_ok=True)
    path = inbox_dir() / f"{timestamp}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"collected_at": timestamp, "alerts": alerts, "processed": False},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def _write_last_collect() -> None:
    try:
        watch_dir().mkdir(parents=True, exist_ok=True)
        last_collect_path().write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - 状态文件写失败不影响采集本身
        pass


def collect_once() -> str:
    """一次拉取巡检。返回人类可读结果串（供 CLI/日志），**永不抛异常**。

    门控（硬约束 3）：``ops.watch.enabled: false`` 或 alertmanager 未配置 →
    直接返回不做任何事；采集失败只写 errors.log。
    """
    if not watch_enabled():
        return "watch 采集已禁用（ops.watch.enabled: false），跳过。"

    prom = _prom_config()
    alertmanager = (prom.get("alertmanager") or "").strip()
    if not alertmanager:
        _log_error("alertmanager 未配置（ops.prometheus.alertmanager 为空），跳过采集")
        return "alertmanager 未配置（ops.prometheus.alertmanager 为空），跳过采集。"

    try:
        alerts = _fetch_alerts(alertmanager, prom)
    except Exception as exc:
        _log_error(f"采集失败: {exc}")
        return f"采集失败: {exc}"

    _write_last_collect()

    if not alerts:
        return "无活跃告警。"

    existing = _unprocessed_keys()
    fresh = [a for a in alerts if _alert_key(a) not in existing]
    if not fresh:
        return (
            f"活跃告警 {len(alerts)} 条但均为未处理 inbox 中已有告警，"
            "按 alertname+instance 去重跳过。"
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ%f")
    _write_inbox(fresh, timestamp)
    return f"写入 {len(fresh)} 条新告警到 inbox（活跃 {len(alerts)} 条，去重跳过 {len(alerts) - len(fresh)} 条）。"


# 便捷别名：循环/测试直接调用
collect = collect_once
