"""UI 监控 API（OPS-DELTA #78）：健康 / PromQL 查询 / 活跃告警。

分层：只打 web_server 新增监控端点（/api/monitoring/health|query|alerts）。
health 用拓扑数据 + 探测函数打桩（不碰真实网络）；query/alerts 用 prom_tools
通道打桩。硬约束断言：端点全部 _require_token（无 token 401）；健康是只读动态
状态（不落盘、不写拓扑）；未配置 Prometheus/Alertmanager → 503 + 明确提示；
空告警 → 200 空列表（不 500）；上游错误信息不吞（502）。
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

import hermes_cli.config as hc
from hermes_cli import web_server
import hermes_cli.monitoring as mon


@pytest.fixture
def client():
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 清监控缓存，避免跨测试串扰。"""
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    mon._health_cache.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    mon._health_cache.clear()


def _write_cfg(home, *, prometheus=True, alertmanager=True) -> None:
    (home / "config.yaml").write_text(
        "ops:\n"
        "  prometheus:\n"
        f"    endpoint: {'http://127.0.0.1:9090' if prometheus else ''}\n"
        f"    alertmanager: {'http://127.0.0.1:9093' if alertmanager else ''}\n"
        "    vault_path: ''\n",
        encoding="utf-8",
    )


def _write_topo(home) -> None:
    """v0.4 拓扑：1 host × 3 服务（http / tcp+extra_port / 无 endpoint）。"""
    (home / "topology.yaml").write_text(json.dumps({
        "version": 4,
        "updated_at": "2026-08-23T00:00:00+08:00",
        "hosts": [{"name": "node1", "env": "prod", "cluster": "k8s-prod",
                   "endpoint": "203.0.113.10"}],
        "clusters": [],
        "environments": [],
    }), encoding="utf-8")
    (home / "services").mkdir()
    (home / "services" / "node1.yaml").write_text(json.dumps({
        "host": "node1",
        "updated_at": "2026-08-23T00:00:00+08:00",
        "services": [
            {"name": "web", "type": "app", "managed_by": "systemd",
             "endpoint": "http://web.local:8080", "extra_ports": []},
            {"name": "db", "type": "db", "managed_by": "docker_compose",
             "endpoint": "node1:5432", "extra_ports": [5433]},
            {"name": "noep", "type": "app", "managed_by": "systemd",
             "endpoint": None, "extra_ports": []},
        ],
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# /api/monitoring/health
# ---------------------------------------------------------------------------

def test_health_requires_token(env_home, client):
    client.headers.pop(web_server._SESSION_HEADER_NAME, None)
    resp = client.get("/api/monitoring/health")
    assert resp.status_code == 401


def test_health_three_states(env_home, client, monkeypatch):
    _write_topo(env_home)
    monkeypatch.setattr(mon, "_probe_http", lambda url: ("up", 1.5))
    monkeypatch.setattr(mon, "_probe_tcp", lambda host, port: (
        ("down", 2.0) if port == 5432 else ("up", 0.8)
    ))
    resp = client.get("/api/monitoring/health")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["summary"] == {"up": 1, "down": 1, "unknown": 1, "internal": 0}
    assert data["cached"] is False
    by_name = {s["name"]: s for s in data["services"]}
    assert by_name["web"]["status"] == "up"
    assert by_name["web"]["latency_ms"] == 1.5
    assert by_name["db"]["status"] == "down"
    ports = {p["port"]: p["status"] for p in by_name["db"]["ports"]}
    assert ports == {5432: "down", 5433: "up"}
    assert by_name["noep"]["status"] == "unknown"
    assert by_name["noep"]["latency_ms"] is None
    assert "checked_at" in data
    # 探测不落盘（services/ 目录之外无新增；拓扑文件未被改写）。
    assert not (env_home / "hosts").exists()
    topo_raw = json.loads((env_home / "topology.yaml").read_text())
    assert topo_raw["version"] == 4


def test_health_http_non_2xx_is_down(env_home, client, monkeypatch):
    _write_topo(env_home)
    monkeypatch.setattr(mon, "_probe_http", lambda url: ("down", 3.0))
    monkeypatch.setattr(mon, "_probe_tcp", lambda host, port: ("up", 0.5))
    resp = client.get("/api/monitoring/health")
    by_name = {s["name"]: s for s in resp.json()["data"]["services"]}
    assert by_name["web"]["status"] == "down"


def test_health_30s_cache_and_force_refresh(env_home, client, monkeypatch):
    _write_topo(env_home)
    calls = {"n": 0}

    def fake_probe_http(url):
        calls["n"] += 1
        return ("up", 1.0)

    monkeypatch.setattr(mon, "_probe_http", fake_probe_http)
    monkeypatch.setattr(mon, "_probe_tcp", lambda host, port: ("up", 0.5))

    r1 = client.get("/api/monitoring/health").json()["data"]
    assert r1["cached"] is False
    assert calls["n"] == 1  # 仅 web 是 HTTP 类

    r2 = client.get("/api/monitoring/health").json()["data"]
    assert r2["cached"] is True
    assert r2["checked_at"] == r1["checked_at"]
    assert calls["n"] == 1  # 缓存命中，不重复探测

    r3 = client.get("/api/monitoring/health?refresh=1").json()["data"]
    assert r3["cached"] is False
    assert calls["n"] == 2  # 强制重探


def test_health_batch_timeout_marks_remaining_unknown(env_home, client, monkeypatch):
    """批次总超时（30s 上限）生效：未完成探测标记 unknown，不误报 down。"""
    _write_topo(env_home)
    monkeypatch.setattr(mon, "_PROBE_BATCH_TIMEOUT", 0.2)
    monkeypatch.setattr(mon, "_PROBE_TIMEOUT", 10.0)

    def slow_probe(host, port):
        time.sleep(2)
        return ("up", 0.0)

    monkeypatch.setattr(mon, "_probe_http", lambda url: time.sleep(2) or ("up", 0.0))
    monkeypatch.setattr(mon, "_probe_tcp", slow_probe)
    start = time.monotonic()
    resp = client.get("/api/monitoring/health")
    elapsed = time.monotonic() - start
    assert resp.status_code == 200
    assert elapsed < 1.5  # 没等慢探测跑完
    data = resp.json()["data"]
    assert data["summary"] == {"up": 0, "down": 0, "unknown": 3, "internal": 0}
    for s in data["services"]:
        assert s["status"] == "unknown"


def test_health_internal_rows_not_probed_and_counted(env_home, client, monkeypatch):
    """reachability=internal（ClusterIP 类内部端口）不探测：状态 internal、
    latency None、无 ports 明细，不计入 up/down；无标记 → 默认 external 照常
    探测（不静默跳过）；大小写/空白容忍（手改 YAML 常见）。"""
    (env_home / "topology.yaml").write_text(json.dumps({
        "version": 4,
        "hosts": [{"name": "node1", "env": "prod", "cluster": "k8s-prod",
                   "endpoint": "203.0.113.10"}],
    }), encoding="utf-8")
    (env_home / "services").mkdir()
    (env_home / "services" / "node1.yaml").write_text(json.dumps({
        "host": "node1",
        "services": [
            {"name": "argocd-redis", "type": "cache", "managed_by": "kubectl",
             "endpoint": "203.0.113.10:6379", "reachability": "internal"},
            {"name": "eventbus", "type": "queue", "managed_by": "kubectl",
             "endpoint": "203.0.113.10:4222", "reachability": "INTERNAL"},
            {"name": "web", "type": "app", "managed_by": "systemd",
             "endpoint": "203.0.113.10:8080"},
        ],
    }), encoding="utf-8")
    tcp_calls: list = []

    def fake_tcp(host, port):
        tcp_calls.append(port)
        return ("up", 0.5)

    monkeypatch.setattr(mon, "_probe_tcp", fake_tcp)
    monkeypatch.setattr(mon, "_probe_http", lambda url: ("up", 1.0))
    resp = client.get("/api/monitoring/health")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["summary"] == {"up": 1, "down": 0, "unknown": 0, "internal": 2}
    by_name = {s["name"]: s for s in data["services"]}
    # internal 行：不发起连接、无延迟、无端口明细。
    assert by_name["argocd-redis"]["status"] == "internal"
    assert by_name["argocd-redis"]["latency_ms"] is None
    assert by_name["argocd-redis"]["ports"] == []
    assert by_name["eventbus"]["status"] == "internal"
    # 无标记行：默认 external，照常探测。
    assert by_name["web"]["status"] == "up"
    assert tcp_calls == [8080]
    assert 6379 not in tcp_calls and 4222 not in tcp_calls


# ---------------------------------------------------------------------------
# /api/monitoring/query
# ---------------------------------------------------------------------------

def _fake_resp(status_code=200, payload=None, text=""):
    return type("_Resp", (), {
        "status_code": status_code,
        "text": text,
        "json": lambda self: payload,
    })()


def test_query_requires_token(env_home, client):
    client.headers.pop(web_server._SESSION_HEADER_NAME, None)
    resp = client.get("/api/monitoring/query", params={"promql": "up"})
    assert resp.status_code == 401


def test_query_unconfigured_503(env_home, client):
    _write_cfg(env_home, prometheus=False)
    resp = client.get("/api/monitoring/query", params={"promql": "up"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "prometheus_unavailable"
    assert "未配置 Prometheus" in body["error"]["message"]


def test_query_parameter_validation(env_home, client, monkeypatch):
    _write_cfg(env_home)
    # promql 必填
    resp = client.get("/api/monitoring/query", params={"promql": "  "})
    assert resp.status_code == 400
    assert "promql" in resp.json()["error"]["message"]
    # PromQL 预校验拒绝 shell
    resp = client.get("/api/monitoring/query", params={"promql": "curl http://x"})
    assert resp.status_code == 400
    assert "预校验失败" in resp.json()["error"]["message"]
    # duration 非法
    resp = client.get("/api/monitoring/query",
                      params={"promql": "up", "duration": "abc"})
    assert resp.status_code == 400
    assert "duration/step" in resp.json()["error"]["message"]


def test_query_upstream_error_passthrough(env_home, client, monkeypatch):
    _write_cfg(env_home)
    import httpx
    from tools import prom_tools as pt

    def raise_timeout(url, params, auth):
        raise httpx.TimeoutException("connect timed out")

    monkeypatch.setattr(pt, "_http_get", raise_timeout)
    resp = client.get("/api/monitoring/query", params={"promql": "up"})
    assert resp.status_code == 502
    assert "connect timed out" in resp.json()["error"]["message"]

    # 上游 HTTP 500：原始 body 透传（不吞）
    monkeypatch.setattr(
        pt, "_http_get",
        lambda url, params, auth: _fake_resp(500, text="prometheus exploded"),
    )
    resp = client.get("/api/monitoring/query", params={"promql": "up"})
    assert resp.status_code == 502
    assert "prometheus exploded" in resp.json()["error"]["message"]

    # 上游 PromQL 错误（status=error）：原始 error 透传
    monkeypatch.setattr(
        pt, "_http_get",
        lambda url, params, auth: _fake_resp(
            200, {"status": "error", "error": "invalid parameter: bad"})
    )
    resp = client.get("/api/monitoring/query", params={"promql": "up"})
    assert resp.status_code == 502
    assert "invalid parameter: bad" in resp.json()["error"]["message"]


def test_query_success_structured_series(env_home, client, monkeypatch):
    _write_cfg(env_home)
    from tools import prom_tools as pt

    payload = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"__name__": "up", "job": "node"},
         "values": [[1724400000.0, "1"], [1724400060.0, "0"], [1724400120.0, "1"]]},
        {"metric": {"__name__": "up", "job": "api"},
         "values": [[1724400000.0, "1"], [1724400060.0, "1"]]},
    ]}}

    monkeypatch.setattr(
        pt, "_http_get",
        lambda url, params, auth: _fake_resp(200, payload),
    )
    resp = client.get("/api/monitoring/query", params={"promql": "up"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["kind"] == "range"
    assert data["duration"] == "30m" and data["step"] == "60s"  # 默认值生效
    assert len(data["series"]) == 2
    s0 = data["series"][0]
    assert s0["name"] == "up"
    assert s0["labels"] == {"job": "node"}
    assert s0["points"] == [[1724400000.0, 1.0], [1724400060.0, 0.0], [1724400120.0, 1.0]]
    assert s0["summary"] == {"min": 0.0, "max": 1.0, "last": 1.0}


def test_query_custom_duration_step(env_home, client, monkeypatch):
    _write_cfg(env_home)
    from tools import prom_tools as pt

    captured = {}

    def fake_get(url, params, auth):
        captured.update(params)
        return _fake_resp(200, {"status": "success", "data": {"result": []}})

    monkeypatch.setattr(pt, "_http_get", fake_get)
    resp = client.get("/api/monitoring/query",
                      params={"promql": "up", "duration": "1h", "step": "5m"})
    assert resp.status_code == 200
    assert captured["step"] == "5m"
    assert "start" in captured and "end" in captured


# ---------------------------------------------------------------------------
# /api/monitoring/alerts
# ---------------------------------------------------------------------------

def test_alerts_requires_token(env_home, client):
    client.headers.pop(web_server._SESSION_HEADER_NAME, None)
    resp = client.get("/api/monitoring/alerts")
    assert resp.status_code == 401


def test_alerts_unconfigured_503(env_home, client):
    _write_cfg(env_home, alertmanager=False)
    resp = client.get("/api/monitoring/alerts")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "alertmanager_unavailable"
    assert "未配置 Alertmanager" in body["error"]["message"]


def test_alerts_empty_list_200(env_home, client, monkeypatch):
    _write_cfg(env_home)
    from tools import prom_tools as pt

    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _fake_resp(200, []))
    resp = client.get("/api/monitoring/alerts")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["count"] == 0
    assert data["alerts"] == []


def test_alerts_maps_fields_and_filters_resolved(env_home, client, monkeypatch):
    _write_cfg(env_home)
    from tools import prom_tools as pt

    payload = [
        {"status": {"state": "active"},
         "labels": {"alertname": "HighCPU", "severity": "critical",
                    "instance": "node1:9100", "job": "node"},
         "startsAt": "2026-08-23T10:00:00Z"},
        {"status": {"state": "suppressed"},
         "labels": {"alertname": "LowDisk", "severity": "warning"},
         "startsAt": "2026-08-23T09:00:00Z"},
        {"status": {"state": "resolved"},
         "labels": {"alertname": "Old", "severity": "info"},
         "startsAt": "2026-08-23T08:00:00Z"},
    ]
    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _fake_resp(200, payload))
    resp = client.get("/api/monitoring/alerts")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["count"] == 2  # resolved 过滤
    first = data["alerts"][0]  # critical 排前
    assert first["alertname"] == "HighCPU"
    assert first["severity"] == "critical"
    assert first["instance"] == "node1:9100"
    assert first["startsAt"] == "2026-08-23T10:00:00Z"
    assert first["state"] == "active"
    assert "job" in first["labels"]


def test_alerts_annotations_carried_through(env_home, client, monkeypatch):
    """batch94 C2：annotations 全量透传（此前丢弃 → 结构化触发词在真实管道
    永远无法命中）。"""
    _write_cfg(env_home)
    from tools import prom_tools as pt

    payload = [{
        "status": {"state": "active"},
        "labels": {"alertname": "HarborUnhealthy", "severity": "critical",
                   "instance": "harbor:443"},
        "annotations": {"summary": "harbor down",
                        "playbook": "harbor-restart",
                        "runbook_url": "http://ops/runbook"},
        "startsAt": "2026-08-23T10:00:00Z",
    }]
    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _fake_resp(200, payload))
    resp = client.get("/api/monitoring/alerts")
    assert resp.status_code == 200
    first = resp.json()["data"]["alerts"][0]
    assert first["annotations"]["playbook"] == "harbor-restart"
    assert first["annotations"]["runbook_url"] == "http://ops/runbook"
    assert first["summary"] == "harbor down"


def test_alerts_upstream_error_502(env_home, client, monkeypatch):
    _write_cfg(env_home)
    import httpx
    from tools import prom_tools as pt

    monkeypatch.setattr(
        pt, "_http_get",
        lambda url, params, auth: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    )
    resp = client.get("/api/monitoring/alerts")
    assert resp.status_code == 502
    assert "refused" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# /api/monitoring/alerts/triage（batch87 OPS-DELTA #103：告警→runbook 处置建议）
# ---------------------------------------------------------------------------

HARBOR_RUNBOOK = """\
name: harbor-restart
title: Harbor 服务异常恢复
kind: incident
env: prod
summary: harbor 健康检查失败时的标准恢复流程。
triggers:
  - harbor healthcheck failed
steps:
  - id: diagnose
    title: 诊断
    commands: ["docker ps --filter name=harbor"]
"""


def _write_triage_runbooks(home) -> None:
    (home / "runbooks").mkdir()
    (home / "runbooks" / "harbor-restart.yaml").write_text(HARBOR_RUNBOOK, encoding="utf-8")


def _am_payload():
    return [
        {"status": {"state": "active"},
         "labels": {"alertname": "Harbor healthcheck failed", "severity": "critical",
                    "instance": "harbor:443"},
         "annotations": {"summary": ""},
         "startsAt": "2026-08-23T10:00:00Z"},
        {"status": {"state": "active"},
         "labels": {"alertname": "DiskFull", "severity": "warning",
                    "instance": "worker-1"},
         "annotations": {"summary": "disk full on worker"},
         "startsAt": "2026-08-23T09:00:00Z"},
    ]


def test_triage_requires_token(env_home, client):
    client.headers.pop(web_server._SESSION_HEADER_NAME, None)
    resp = client.get("/api/monitoring/alerts/triage")
    assert resp.status_code == 401


def test_triage_unconfigured_alertmanager_503(env_home, client):
    _write_cfg(env_home, alertmanager=False)
    resp = client.get("/api/monitoring/alerts/triage")
    assert resp.status_code == 503
    assert "alertmanager_unavailable" == resp.json()["error"]["code"]


def test_triage_structure_and_audit(env_home, client, monkeypatch):
    _write_cfg(env_home)
    _write_triage_runbooks(env_home)
    from tools import prom_tools as pt

    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _fake_resp(200, _am_payload()))
    resp = client.get("/api/monitoring/alerts/triage")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["count"] == 2
    assert data["matched_count"] == 1
    assert data["unmatched_count"] == 1
    by_name = {a["alertname"]: a for a in data["alerts"]}
    hit = by_name["Harbor healthcheck failed"]["disposition"]
    assert hit["matched"] is True
    assert hit["runbook"] == "harbor-restart"
    assert hit["confidence"] == "high"
    assert hit["matched_by"] == "trigger"
    miss = by_name["DiskFull"]["disposition"]
    assert miss["matched"] is False
    assert "无匹配 runbook" in miss["hint"]
    # 每次调用落一条审计（m/n）：runtime/alert_triage.jsonl 一条，可读。
    audit = env_home / "runtime" / "alert_triage.jsonl"
    assert audit.is_file()
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["type"] == "alert_triage"
    assert rows[0]["matched_count"] == 1


def test_triage_readonly_no_runbook_execution(env_home, client, monkeypatch):
    """l 验收：triage 只读——不产生 runbook 执行账本、不触碰执行链。"""
    _write_cfg(env_home)
    _write_triage_runbooks(env_home)
    from tools import prom_tools as pt

    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _fake_resp(200, _am_payload()))
    resp = client.get("/api/monitoring/alerts/triage")
    assert resp.status_code == 200
    assert resp.json()["data"]["matched_count"] == 1
    assert not (env_home / "runtime" / "runbook_executions.jsonl").exists()


def test_triage_upstream_error_502(env_home, client, monkeypatch):
    _write_cfg(env_home)
    import httpx
    from tools import prom_tools as pt

    monkeypatch.setattr(
        pt, "_http_get",
        lambda url, params, auth: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    )
    resp = client.get("/api/monitoring/alerts/triage")
    assert resp.status_code == 502
    assert "refused" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# batch94 C2 — 端到端：Alertmanager annotations → fetch_active_alerts →
# alert_runbook 结构化触发词命中（真实管道此前断链）
# ---------------------------------------------------------------------------

def test_annotations_end_to_end_into_matcher(env_home, monkeypatch):
    _write_cfg(env_home)
    (env_home / "runbooks").mkdir(exist_ok=True)
    (env_home / "runbooks" / "ann-playbook.yaml").write_text("""\
name: ann-playbook
title: 注解路由 SOP
kind: incident
env: prod
summary: 按 annotations.playbook 路由。
triggers:
  - {playbook: harbor-restart}
steps:
  - id: check
    title: 探查
    action: query
    params: {pattern: harbor}
""", encoding="utf-8")
    from tools import prom_tools as pt
    from tools import alert_runbook as ar

    payload = [{
        "status": {"state": "active"},
        "labels": {"alertname": "HarborUnhealthy", "severity": "critical",
                   "instance": "harbor:443"},
        "annotations": {"summary": "harbor down", "playbook": "harbor-restart"},
        "startsAt": "2026-08-23T10:00:00Z",
    }]
    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _fake_resp(200, payload))
    alerts = mon.fetch_active_alerts()["alerts"]
    assert alerts[0]["annotations"]["playbook"] == "harbor-restart"
    disp = ar.match_alert_to_runbook(alerts[0], home=env_home)
    assert disp["matched"] is True
    assert disp["matched_by"] == "trigger"
    assert disp["matched_keyword"] == "playbook=harbor-restart"
