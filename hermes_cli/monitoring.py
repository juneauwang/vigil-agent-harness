"""UI 监控 API 后端（OPS-DELTA #78，YAPL 之后第一个新功能面）。

通用监控展示层：dashboard 让人看到监控数据——服务健康（不依赖 Prometheus，
开箱即用）+ PromQL 查询 + Alertmanager 活跃告警（后两者复用 ops.prometheus
配置段，零新配置；未配置 → 503 + 明确提示）。

定位（设计定案）：
- 服务健康是**动态状态，按需探测、不落盘**（设计 §9.1「静态事实进拓扑，
  动态状态按需探测」）——不写拓扑文件、不产生审计、凭据明文永不落日志。
- 探测语义复用 P4 执行器检查通道（http_status / port）的判定规则，但**独立
  轻量实现**，不依赖 runbook 执行器（避免耦合）。
- 30s 短缓存（内存 dict + TTL）——页面刷新不重复探测，缓存过期自动重探。

探测规则：
- HTTP 类 endpoint（http:// 或 https://）→ HTTP GET，200-399 = up；
  超时/连接失败 = down。
- 端口类 endpoint（host:port）→ TCP 连接测试，通 = up。
- 无法探测（无 endpoint / 无端口 / 需认证的服务）→ unknown（不误报）。
- extra_ports 随主 endpoint 一并探测，作为补充信息返回（不改变主状态）。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# 探测参数（任务书定案）：并发上限 5、单次超时 3s、批次总超时 30s。
_PROBE_CONCURRENCY = 5
_PROBE_TIMEOUT = 3.0
_PROBE_BATCH_TIMEOUT = 30.0
# 30s 短缓存（内存 dict + TTL）。
_HEALTH_CACHE_TTL = 30.0
# 每个服务 extra_ports 探测上限（防单服务拖垮批次）。
_MAX_EXTRA_PORTS_PROBED = 8

# query 端点默认参数。
_DEFAULT_DURATION = "30m"
_DEFAULT_STEP = "60s"
# range 查询返回上限：最多 series 数 / 每 series 时间点数（前端 sparkline 采样）。
_MAX_SERIES = 20
_MAX_POINTS = 120
# 上游错误文本截断（与 prom_tools 一致，防超大响应撑爆响应体）。
_MAX_ERROR_CHARS = 500

_HTTP_OK_STATUSES = range(200, 400)


class MonitoringUnavailable(Exception):
    """Prometheus/Alertmanager 未配置或配置不可解析（→ 503）。"""


class MonitoringBadRequest(Exception):
    """参数非法（→ 400）。"""


class MonitoringUpstreamError(Exception):
    """上游查询超时/错误（→ 502），message 含上游原始信息（不吞）。"""


# ---------------------------------------------------------------------------
# 服务健康（拓扑只读 + 并发探测 + 30s 缓存）
# ---------------------------------------------------------------------------

_health_cache: Dict[str, Dict[str, Any]] = {}
_health_cache_lock = threading.Lock()


def _service_rows(home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """枚举拓扑服务（P1 数据层只读）：topology.yaml + services/<host>.yaml。

    每行补 host/cluster/env 来源标记（沿用 topo_tools._all_services 的 _host
    语义）；endpoint 缺失不影响枚举（探测层 unknown 兜底，不误报）。
    """
    from hermes_constants import get_hermes_home
    from tools.topo_tools import _all_services, load_topology

    base = Path(home) if home is not None else Path(get_hermes_home())
    topo = load_topology(base)
    if topo is None:
        return []
    rows: List[Dict[str, Any]] = []
    for row in _all_services(topo, base):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        rows.append({
            "name": name,
            "host": str(row.get("_host") or "").strip(),
            "cluster": str(row.get("cluster") or "").strip() or "default",
            "type": str(row.get("type") or "service").strip() or "service",
            "managed_by": str(row.get("managed_by") or "").strip(),
            "env": str(row.get("env") or "").strip(),
            "endpoint": str(row.get("endpoint") or "").strip() or None,
            "extra_ports": _normalize_ports(row.get("extra_ports")),
        })
    return rows


def _normalize_ports(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    out: List[int] = []
    for p in value:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 0 < n < 65536 and n not in out:
            out.append(n)
    return out


def _split_endpoint(endpoint: str) -> tuple[str, str, Optional[int]]:
    """endpoint → (kind, target, port)。

    kind ∈ http/https/tcp；tcp 的 port 为 None 表示无端口（无法探测 → unknown）。
    """
    ep = (endpoint or "").strip()
    low = ep.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return (low.split("://", 1)[0], ep, None)
    host, sep, port = ep.rpartition(":")
    if sep and host and port.isdigit():
        return ("tcp", host, int(port))
    return ("tcp", ep, None)


def _endpoint_host(endpoint: str) -> Optional[str]:
    """提取 endpoint 的主机部分（extra_ports 探测共用）。"""
    kind, target, _ = _split_endpoint(endpoint or "")
    if kind in ("http", "https"):
        from urllib.parse import urlparse
        return urlparse(target).hostname or None
    return target if target else None


def _probe_http(url: str) -> tuple[str, float]:
    start = time.monotonic()
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
        return ("up" if resp.status_code in _HTTP_OK_STATUSES else "down",
                round((time.monotonic() - start) * 1000.0, 1))
    except Exception:
        return ("down", round((time.monotonic() - start) * 1000.0, 1))


def _probe_tcp(host: str, port: int) -> tuple[str, float]:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            pass
        return ("up", round((time.monotonic() - start) * 1000.0, 1))
    except Exception:
        return ("down", round((time.monotonic() - start) * 1000.0, 1))


def _probe_service(row: Dict[str, Any]) -> Dict[str, Any]:
    """单服务探测：主 endpoint 定状态，extra_ports 补充信息。"""
    ep = row.get("endpoint")
    ports: List[Dict[str, Any]] = []
    status = "unknown"
    latency_ms: Optional[float] = None
    if ep:
        kind, target, port = _split_endpoint(ep)
        if kind in ("http", "https"):
            s, lat = _probe_http(target)
            status, latency_ms = s, lat
            ports.append({"port": None, "proto": "http", "status": s, "latency_ms": lat})
        elif port is not None:
            s, lat = _probe_tcp(target, port)
            status, latency_ms = s, lat
            ports.append({"port": port, "proto": "tcp", "status": s, "latency_ms": lat})
        # 无端口 host → 无法探测 → unknown（不误报）。
        host = _endpoint_host(ep)
        for p in (row.get("extra_ports") or [])[:_MAX_EXTRA_PORTS_PROBED]:
            if not host:
                break
            s, lat = _probe_tcp(host, int(p))
            ports.append({"port": int(p), "proto": "tcp", "status": s, "latency_ms": lat})
    return {**row, "status": status, "latency_ms": latency_ms, "ports": ports}


def probe_services_health(home: Optional[Path] = None,
                          refresh: bool = False) -> Dict[str, Any]:
    """拓扑服务健康探测（只读；30s 短缓存，refresh=True 强制重探）。"""
    from hermes_constants import get_hermes_home

    base = str(Path(home) if home is not None else Path(get_hermes_home()))
    now = time.monotonic()
    with _health_cache_lock:
        cached = _health_cache.get(base)
        if cached and not refresh and now - cached["_at"] < _HEALTH_CACHE_TTL:
            out = dict(cached)
            out.pop("_at", None)
            out["cached"] = True
            return out

    rows = _service_rows(Path(base))
    checked_at = datetime.now(timezone.utc).isoformat()
    results: List[Dict[str, Any]] = []
    deadline = time.monotonic() + _PROBE_BATCH_TIMEOUT
    if rows:
        pool = ThreadPoolExecutor(max_workers=_PROBE_CONCURRENCY)
        try:
            futs = {pool.submit(_probe_service, row): row for row in rows}
            remaining = max(0.1, deadline - time.monotonic())
            done: set = set()
            try:
                for fut in as_completed(futs, timeout=remaining):
                    done.add(fut)
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # 探测本身不抛，防御兜底
                        results.append({**futs[fut], "status": "unknown",
                                        "latency_ms": None, "ports": [],
                                        "error": str(exc)[:_MAX_ERROR_CHARS]})
            except TimeoutError:
                pass  # 批次超时：未完成探测标记 unknown（不误报 down）。
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        pending = [futs[f] for f in futs if f not in done]
        for row in pending:
            results.append({**row, "status": "unknown", "latency_ms": None,
                            "ports": [], "error": "探测批次超时（30s）"})
    # 保序（与拓扑顺序一致），未完成行补在最后。
    by_name = {r["name"]: r for r in results}
    ordered = [by_name.get(r["name"], r) for r in rows]
    summary = {"up": 0, "down": 0, "unknown": 0}
    for r in ordered:
        key = r.get("status")
        if key in summary:
            summary[key] += 1
    entry = {
        "_at": time.monotonic(),
        "checked_at": checked_at,
        "cached": False,
        "summary": summary,
        "services": ordered,
    }
    with _health_cache_lock:
        _health_cache[base] = entry
    out = dict(entry)
    out.pop("_at", None)
    return out


# ---------------------------------------------------------------------------
# PromQL 查询 / Alertmanager 活跃告警（复用 prom_tools 只读通道）
# ---------------------------------------------------------------------------

def _summarize_series(series: Dict[str, Any]) -> Dict[str, Any]:
    """Prometheus range result series → 结构化（时间点 + min/max/last 摘要）。"""
    metric = series.get("metric") or {}
    values = series.get("values") or []
    points: List[list] = []
    valid: List[float] = []
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            ts = float(item[0])
            v = float(item[1])
        except (TypeError, ValueError):
            points.append([float(item[0]), None])
            continue
        points.append([ts, v])
        if v == v:  # 非 NaN
            valid.append(v)
    summary: Dict[str, Any] = {}
    if valid:
        summary = {"min": min(valid), "max": max(valid), "last": valid[-1]}
    if len(points) > _MAX_POINTS:
        stride = max(1, len(points) // _MAX_POINTS)
        points = points[::stride][:_MAX_POINTS]
    labels = {str(k): str(v) for k, v in metric.items() if k != "__name__"}
    return {
        "name": str(metric.get("__name__") or ""),
        "labels": labels,
        "points": points,
        "summary": summary,
        "point_count": len(values),
    }


def query_prometheus(promql: str, duration: str = "", step: str = "") -> Dict[str, Any]:
    """PromQL range 查询（结构化 series；未配置 → MonitoringUnavailable）。

    duration/step 有默认值（30m/60s）；非法参数 → MonitoringBadRequest；
    上游超时/错误 → MonitoringUpstreamError（上游原始信息不吞）。
    """
    from tools import prom_tools as pt

    cfg = pt._prom_config()
    endpoint = pt._endpoint_url(cfg)
    if not endpoint:
        raise MonitoringUnavailable(
            "未配置 Prometheus（config ops.prometheus.endpoint），仅健康探测可用。"
        )
    q = str(promql or "").strip()
    if not q:
        raise MonitoringBadRequest("promql 必填（PromQL 表达式）。")
    invalid = pt._validate_promql(q)
    if invalid:
        raise MonitoringBadRequest(f"PromQL 预校验失败：{invalid}")

    auth: Optional[str] = None
    try:
        auth = pt._resolve_basic_auth(cfg)
    except ValueError as exc:
        raise MonitoringUnavailable(str(exc)) from None

    duration = str(duration or _DEFAULT_DURATION).strip()
    step = str(step or _DEFAULT_STEP).strip()
    if not pt._DURATION_RE.fullmatch(duration) or not pt._DURATION_RE.fullmatch(step):
        raise MonitoringBadRequest(
            "duration/step 格式非法（应为 30s/1m/1h/24h 等时长）。"
        )
    try:
        end = datetime.now(timezone.utc)
        start = end - pt._parse_duration(duration)
    except ValueError as exc:
        raise MonitoringBadRequest(str(exc)) from None

    params = {
        "query": q,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "step": step,
    }
    url = f"{endpoint}/api/v1/query_range"
    try:
        resp = pt._http_get(url, params, auth)
    except httpx.TimeoutException as exc:
        raise MonitoringUpstreamError(f"Prometheus 请求超时: {exc}") from None
    except httpx.HTTPError as exc:
        raise MonitoringUpstreamError(f"Prometheus 请求失败: {exc}") from None
    if resp.status_code != 200:
        raise MonitoringUpstreamError(
            f"Prometheus 查询失败 HTTP {resp.status_code}: {resp.text[:_MAX_ERROR_CHARS]}"
        )
    try:
        payload = resp.json()
    except ValueError:
        raise MonitoringUpstreamError(
            f"Prometheus 返回非 JSON: {resp.text[:_MAX_ERROR_CHARS]}"
        ) from None
    status = payload.get("status")
    if status == "error":
        raise MonitoringUpstreamError(f"Prometheus 查询错误: {payload.get('error', 'unknown')}")
    if status != "success":
        raise MonitoringUpstreamError(f"Prometheus 响应异常 status={status!r}")

    data = payload.get("data") or {}
    result = data.get("result") or []
    if not isinstance(result, list):
        raise MonitoringUpstreamError("Prometheus 响应缺少 result 列表。")
    series = [_summarize_series(s) for s in result[:_MAX_SERIES]]
    return {
        "kind": "range",
        "query": q,
        "duration": duration,
        "step": step,
        "series": series,
        "truncated": len(result) > _MAX_SERIES,
    }


def fetch_active_alerts() -> Dict[str, Any]:
    """Alertmanager /api/v2/alerts 实时快照（未配置 → MonitoringUnavailable）。"""
    from tools import prom_tools as pt

    cfg = pt._prom_config()
    alertmanager = (cfg.get("alertmanager") or "").strip().rstrip("/")
    if not alertmanager:
        raise MonitoringUnavailable(
            "未配置 Alertmanager（config ops.prometheus.alertmanager），仅健康探测可用。"
        )
    auth: Optional[str] = None
    try:
        auth = pt._resolve_basic_auth(cfg)
    except ValueError as exc:
        raise MonitoringUnavailable(str(exc)) from None

    url = f"{alertmanager}/api/v2/alerts"
    try:
        resp = pt._http_get(url, {}, auth)
    except httpx.TimeoutException as exc:
        raise MonitoringUpstreamError(f"Alertmanager 请求超时: {exc}") from None
    except httpx.HTTPError as exc:
        raise MonitoringUpstreamError(f"Alertmanager 请求失败: {exc}") from None
    if resp.status_code != 200:
        raise MonitoringUpstreamError(
            f"Alertmanager 查询失败 HTTP {resp.status_code}: {resp.text[:_MAX_ERROR_CHARS]}"
        )
    try:
        alerts = resp.json()
    except ValueError:
        raise MonitoringUpstreamError(
            f"Alertmanager 返回非 JSON: {resp.text[:_MAX_ERROR_CHARS]}"
        ) from None
    if not isinstance(alerts, list):
        raise MonitoringUpstreamError("Alertmanager 响应不是告警列表。")

    out: List[Dict[str, Any]] = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        status = a.get("status") or {}
        if str(status.get("state") or "").lower() == "resolved":
            continue
        labels = a.get("labels") or {}
        out.append({
            "alertname": str(labels.get("alertname") or a.get("name") or "未知告警"),
            "severity": str(labels.get("severity") or "info"),
            "instance": str(labels.get("instance") or ""),
            "labels": {str(k): str(v) for k, v in labels.items() if k != "alertname"},
            "startsAt": a.get("startsAt") or "",
            "state": str(status.get("state") or ""),
        })
    out.sort(key=lambda x: (x["severity"] != "critical", x["severity"] != "warning"))
    return {"alerts": out, "count": len(out)}
