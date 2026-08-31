"""``vigil topo export`` 的 Web UI 壳第一批 API 验收测试。

覆盖：``GET /api/topology``（有数据/无数据/凭据过滤/数据根脱敏）、
``GET /api/runbooks``（列表元数据）、``GET /api/runbooks/{name}``
（详情递归值级脱敏/缺失/路径穿越尝试）、端点无需 session token（公开只读）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

TOPOLOGY_YAML = """\
version: 3
sources: [terraform.tfstate, agent, manual]
environments:
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k8s-prod, env: prod, description: 生产 k3s 集群, owner: your-name}
hosts:
  - {name: node1, env: prod, cluster: k8s-prod, endpoint: "203.0.113.10", role: control-plane, runtime: k3s, status: running, services_index: hosts/node1.yaml, credential: {type: ssh_key, ref: "~/.ssh/aliyun_nopass.pem", user: root}}
cross_host:
  - {name: ingress, type: ingress, env: prod, cluster: k8s-prod, detail: entities/k8s-prod__ingress.yaml, credential: {type: askpass, ref: "/tmp/ask.sh", user: root}}
key_paths:
  - [ingress, gateway-svc, order-db]
"""

HOSTS_NODE1 = """\
host: node1
env: prod
cluster: k8s-prod
services:
  - {name: harbor, type: registry, env: prod, endpoint: "203.0.113.10:30443", detail: entities/k8s-prod__node1__harbor.yaml}
  - {name: gateway-svc, type: service, env: prod, endpoint: "203.0.113.10:30080", status: running}
"""

ENTITY_HARBOR = """\
name: harbor
type: registry
env: prod
attrs:
  version: "v2.11"
  user: dbadmin
  credential: {type: ssh_key, ref: "/home/ops/.ssh/db.pem", user: ops}
ops:
  healthcheck: "curl -s http://localhost/api/v2.0/health"
"""

ENTITY_INGRESS = """\
name: ingress
type: ingress
env: prod
depends_on: [gateway-svc]
"""

RUNBOOK = """\
name: harbor-restart
title: "Harbor 服务异常恢复"
version: 1
env: prod
kind: incident
triggers:
  - "harbor healthcheck failed"
  - "harbor 健康检查失败"
summary: "harbor 健康检查失败时的标准恢复流程"
steps:
  - id: diagnose
    title: 诊断
    commands:
      - "docker ps --filter name=harbor"
      - "ssh -i /home/ops/.ssh/id_rsa root@203.0.113.10 'docker logs harbor'"
  - id: verify
    title: 真实验证
    verify: "curl -s http://localhost/api/v2.0/health"
    expect: '"status":"healthy"'
rollback:
  - title: 兜底
    commands:
      - "docker logs --tail 100 harbor"
"""


@pytest.fixture
def ops_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(TOPOLOGY_YAML, encoding="utf-8")
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "node1.yaml").write_text(HOSTS_NODE1, encoding="utf-8")
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR, encoding="utf-8")
    (tmp_path / "entities" / "k8s-prod__ingress.yaml").write_text(
        ENTITY_INGRESS, encoding="utf-8")
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "harbor-restart.yaml").write_text(RUNBOOK, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli import web_server
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    # 与现有 web_server 测试一致：默认带 loopback session token；公开端点
    # 的无 token 场景单独测（去掉 header 即可）。
    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    yield test_client


# ---------------------------------------------------------------------------
# /api/topology
# ---------------------------------------------------------------------------

def test_topology_endpoint_returns_view(ops_home, client):
    resp = client.get("/api/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert [c["name"] for c in data["clusters"]] == ["k8s-prod"]
    assert [h["card"]["name"] for h in data["hosts"]] == ["node1"]
    assert data["hosts"][0]["services"][0]["card"]["name"] == "harbor"
    assert [c["card"]["name"] for c in data["cross_host"]] == ["ingress"]
    assert data["key_paths"] == [["ingress", "gateway-svc", "order-db"]]
    assert "details" in data and "service:node1:harbor" in data["details"]


def test_topology_endpoint_no_data(ops_home, client, tmp_path, monkeypatch):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(empty))
    resp = client.get("/api/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "topo-discover" in body["error"]


@pytest.mark.parametrize("leak", [
    "aliyun_nopass", ".pem", "~/.ssh", "db.pem", "/home/ops",
    "dbadmin", "ask.sh", "/tmp/ask.sh", "credential",
])
def test_topology_endpoint_credential_leak_free(ops_home, client, leak):
    resp = client.get("/api/topology")
    assert resp.status_code == 200
    raw = json.dumps(resp.json(), ensure_ascii=False)
    assert leak not in raw, f"拓扑 API 泄露: {leak!r}"


def test_topology_endpoint_masks_data_root_home(ops_home, client, tmp_path, monkeypatch):
    """数据根在 home 下 → 显示为 ~ 相对（分享脱敏，绝对路径含用户名不进 API）。"""
    from pathlib import Path as _Path

    fake_home = tmp_path / "home"
    data_root = fake_home / ".vigil"
    data_root.mkdir(parents=True)
    (data_root / "topology.yaml").write_text(TOPOLOGY_YAML, encoding="utf-8")
    (data_root / "hosts").mkdir()
    (data_root / "hosts" / "node1.yaml").write_text(HOSTS_NODE1, encoding="utf-8")
    (data_root / "entities").mkdir()
    (data_root / "entities" / "k8s-prod__node1__harbor.yaml").write_text(
        ENTITY_HARBOR, encoding="utf-8")
    (data_root / "entities" / "k8s-prod__ingress.yaml").write_text(
        ENTITY_INGRESS, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(data_root))
    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: fake_home))

    resp = client.get("/api/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["data_root"] == "~/.vigil"
    raw = json.dumps(body, ensure_ascii=False)
    assert str(fake_home) not in raw


# ---------------------------------------------------------------------------
# /api/runbooks
# ---------------------------------------------------------------------------

def test_runbooks_endpoint_list(ops_home, client):
    resp = client.get("/api/runbooks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["count"] == 1
    rb = data["runbooks"][0]
    assert rb["name"] == "harbor-restart"
    assert rb["title"] == "Harbor 服务异常恢复"
    assert rb["summary"]
    assert rb["triggers"] == ["harbor healthcheck failed", "harbor 健康检查失败"]
    assert rb["step_count"] == 2
    assert rb["updated_at"] is not None
    # 列表只含元数据，不含步骤命令。
    raw = json.dumps(body, ensure_ascii=False)
    assert "docker ps" not in raw and "id_rsa" not in raw


def test_runbooks_endpoint_detail_redacted(ops_home, client):
    resp = client.get("/api/runbooks/harbor-restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["title"] == "Harbor 服务异常恢复"
    assert len(data["steps"]) == 2
    # 步骤命令仍在（结构保留），但凭据样值被脱敏。
    raw = json.dumps(body, ensure_ascii=False)
    assert "docker ps" in raw
    for leak in ("id_rsa", "/home/ops", ".ssh", "aliyun_nopass", ".pem"):
        assert leak not in raw, f"runbook 详情泄露: {leak!r}"


def test_runbooks_endpoint_detail_missing(ops_home, client):
    resp = client.get("/api/runbooks/nope")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "runbook 不存在" in body["error"]


def test_runbooks_endpoint_no_data(ops_home, client, tmp_path, monkeypatch):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(empty))
    resp = client.get("/api/runbooks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "runbook" in body["error"]


def test_runbooks_detail_path_traversal_blocked(ops_home, client):
    """路径穿越尝试：不得返回任何 runbook 数据（404 或 ok:false 均可）。"""
    for name in ("..%2F..", "..", "evil%2Fname"):
        resp = client.get(f"/api/runbooks/{name}")
        if resp.status_code == 404:
            continue
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is False


def test_runbook_detail_vault_placeholder_preserved(ops_home, client):
    """<vault:...> 是凭据引用占位符（非明文），保留展示。"""
    from pathlib import Path as _Path
    home = _Path(os.environ["VIGIL_HOME"])
    (home / "runbooks" / "with-vault.yaml").write_text(
        RUNBOOK.replace(
            '- "docker ps --filter name=harbor"',
            '- "docker ps --filter name=harbor; echo <vault:db/pass>"',
        ),
        encoding="utf-8",
    )
    resp = client.get("/api/runbooks/with-vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "<vault:db/pass>" in json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 公开只读（无需 session token）
# ---------------------------------------------------------------------------

def test_ops_endpoints_public_no_token(ops_home):
    """拓扑/runbook 端点是公开只读：无 session token header 也能 curl。"""
    from starlette.testclient import TestClient
    from hermes_cli.web_server import app

    bare = TestClient(app)
    # 显式不带 _SESSION_HEADER_NAME。
    for url in ("/api/topology", "/api/runbooks", "/api/runbooks/harbor-restart"):
        resp = bare.get(url)
        assert resp.status_code == 200, f"{url} -> {resp.status_code}"
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# /api/health uptime（第三批顶部栏「运行时长」徽标数据源）
# ---------------------------------------------------------------------------

def test_health_reports_uptime_seconds(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body.get("uptime_seconds"), int)
    assert body["uptime_seconds"] >= 0


# ---------------------------------------------------------------------------
# 批八十五（OPS-DELTA #101）—— POST /api/topology/reset（清空拓扑，破坏性）
# ---------------------------------------------------------------------------

def test_topology_reset_requires_explicit_confirm(client, ops_home):
    """无 confirm: true → 400（破坏性操作不静默执行）。"""
    resp = client.post("/api/topology/reset", json={})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["error"]["message"]
    assert (ops_home / "topology.yaml").is_file(), "未确认不得清空"


def test_topology_reset_clears_and_audits(client, ops_home, monkeypatch):
    """confirm: true → 清空全部拓扑数据（拓扑文件删除、非拓扑保留）+ 审计。"""
    events = []
    monkeypatch.setattr("agent.trajectory.record_event",
                        lambda **kw: events.append(kw))
    resp = client.post("/api/topology/reset", json={"confirm": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["hosts"] == 1
    assert data["clusters"] == 1
    assert not (ops_home / "topology.yaml").exists()
    assert not (ops_home / "hosts").exists()
    assert not (ops_home / "entities").exists()
    assert not (ops_home / "services").exists()
    # 非拓扑数据保留
    assert (ops_home / "runbooks" / "harbor-restart.yaml").is_file()
    assert events, "reset 必须写审计记录"
    ev = events[-1]
    assert ev["type"] == "topo_reset"
    assert ev["meta"]["source"] == "ui"
    assert "topology.yaml" in ev["meta"]["removed"]


def test_topology_reset_idempotent_empty(client, tmp_path, monkeypatch):
    """无拓扑数据时 reset 幂等成功（0 计数）。"""
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    resp = client.post("/api/topology/reset", json={"confirm": True})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True
    assert data["hosts"] == 0 and data["clusters"] == 0 and data["entities"] == 0
