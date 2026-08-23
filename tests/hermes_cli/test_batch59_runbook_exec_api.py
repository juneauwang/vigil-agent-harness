"""YAPL P4（批次五十九）：runbook 执行 API（dashboard 触发）+ 执行历史端点验收。

覆盖（任务书 §P4 阶段 5 测试清单）：
  - ``POST /api/runbook/executions``：v0.2 执行接线（scheduled=False 交互路径、
    home 注入、结果回传）；v0.1 拒绝（老路径照旧）；缺失/名称非法 → 4xx；
  - 审批门 web 接线：矩阵 required 步骤 → 注册 web 审批卡 → 批准后继续执行
    （wait_web_approval 打桩 "once"，命令执行通道打桩——引擎真实路径跑通）；
  - ``GET /api/runbook/executions``：空列表 / 有记录（事后审计视图）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def ehome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 拓扑 fixture（nginx docker_compose 服务）+ 矩阵 t1。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text("""
version: 4
environments:
- name: local
clusters:
- name: local
  type: docker
  env: local
  host_groups: []
hosts:
- name: LAPTOP-T2JA2ERE
  type: host
  env: local
  cluster: local
  endpoint: 172.18.120.67
  os: Ubuntu 24.04.3 LTS
  credentials: []
""", encoding="utf-8")
    (home / "services" / "LAPTOP-T2JA2ERE.yaml").write_text("""
host: LAPTOP-T2JA2ERE
services:
- name: nginx
  type: gateway
  managed_by: docker_compose
  source: manual
  detail: entities/nginx.yaml
""", encoding="utf-8")
    (home / "entities" / "nginx.yaml").write_text("""
name: nginx
snapshot:
  by_runtime:
    docker_compose:
      project: docker
      services:
      - name: docker-nginx-1
  common:
    config_dir: /etc/nginx
""", encoding="utf-8")
    from tools.matrix_data import template_matrix, write_matrix
    write_matrix(template_matrix("template1"), home)
    (home / "runbooks" / "t-v2.yaml").write_text("""
name: t-v2
title: T
version: 2
kind: maintenance
env: local
steps:
- id: q
  title: q
  action: query
  params:
    target: nginx
    pattern: x
""", encoding="utf-8")
    (home / "runbooks" / "t-v1.yaml").write_text("""
name: t-v1
title: T1
version: 1
kind: incident
steps:
- id: q
  title: q
  commands: ["ps aux | grep x"]
""", encoding="utf-8")
    return home


@pytest.fixture
def client(monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    yield test_client


def _rb():
    return {
        "name": "t-v2", "title": "T", "version": 2, "kind": "maintenance",
        "env": "local",
        "steps": [{"id": "q", "title": "q", "action": "query",
                   "params": {"target": "nginx", "pattern": "x"}}],
    }


# ---------------------------------------------------------------------------
# GET /api/runbook/executions（执行历史，事后审计视图）
# ---------------------------------------------------------------------------

def test_executions_history_empty(ehome, client):
    resp = client.get("/api/runbook/executions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"count": 0, "executions": []}


def test_executions_history_rows(ehome, client):
    runtime = ehome / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "runbook_executions.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-23T10:00:00+08:00", "runbook": "t-v2", "env": "local",
            "source": "user", "trigger_context": {"source": "user"},
            "result": "ok", "error": None, "rolled_back": False,
            "steps": [{"id": "q", "action": "query", "status": "ok", "ok": True}],
            "duration_s": 0.2, "operator": "tester",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    resp = client.get("/api/runbook/executions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    rows = body["data"]["executions"]
    assert len(rows) == 1
    assert rows[0]["runbook"] == "t-v2"
    assert rows[0]["result"] == "ok"
    assert rows[0]["source"] == "user"


# ---------------------------------------------------------------------------
# POST /api/runbook/executions（交互执行接线）
# ---------------------------------------------------------------------------

def test_exec_post_missing_name(ehome, client):
    resp = client.post("/api/runbook/executions", json={})
    assert resp.status_code == 400


def test_exec_post_bad_name(ehome, client):
    resp = client.post("/api/runbook/executions",
                       json={"name": "../../etc/passwd"})
    assert resp.status_code == 400


def test_exec_post_not_found(ehome, client):
    resp = client.post("/api/runbook/executions", json={"name": "ghost"})
    assert resp.status_code == 404


def test_exec_post_v1_rejected(ehome, client, monkeypatch):
    """v0.1 runbook 走老执行路径——新执行器拒绝，execute_runbook 不被调用。"""
    calls = []
    monkeypatch.setattr(
        "tools.runbook_exec.execute_runbook",
        lambda *a, **k: calls.append((a, k)) or {},
    )
    resp = client.post("/api/runbook/executions", json={"name": "t-v1"})
    assert resp.status_code == 400
    assert "v0.1" in resp.json()["error"]["message"]
    assert calls == []


def test_exec_post_happy_wiring(ehome, client, monkeypatch):
    """v0.2 执行接线：加载 runbook → execute_runbook(scheduled=False, home) → 回传结果。"""
    calls = {}

    def fake_execute(data, *, env="", trigger_context=None, home=None,
                     runner=None, scheduled=False):
        calls["data"] = data
        calls["env"] = env
        calls["scheduled"] = scheduled
        calls["home"] = home
        return {
            "runbook": data.get("name"), "env": env, "result": "ok",
            "error": None, "rolled_back": False, "duration_s": 0.5,
            "steps": [{"id": "q", "action": "query", "status": "ok", "ok": True}],
        }

    monkeypatch.setattr("tools.runbook_exec.execute_runbook", fake_execute)
    resp = client.post("/api/runbook/executions", json={"name": "t-v2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["result"] == "ok"
    assert calls["scheduled"] is False
    assert calls["env"] == ""
    assert calls["home"] == ehome
    assert calls["data"]["name"] == "t-v2"


def test_exec_post_approval_gate_web(ehome, client, monkeypatch):
    """矩阵 required 步骤 → web 审批注册 → 批准（once）→ 继续执行 → 记录落盘。"""
    from tools import terminal_tool
    from tools.approval import list_web_approvals
    from tools.matrix_data import load_matrix, set_level, write_matrix

    m = load_matrix(ehome)
    set_level(m, "local", "query", "required")
    write_matrix(m, ehome)

    monkeypatch.setattr("tools.approval.wait_web_approval",
                        lambda *a, **k: "once")
    monkeypatch.setattr(
        "tools.runbook_exec._run_spec",
        lambda *a, **k: {"exit_code": 0, "stdout": "ok", "stderr": ""},
    )
    # 交互上下文：web 端点线程内 set_hermes_interactive_context(True) ——
    # 引擎审批门应注册 web 审批卡而不是 CLI 提示。
    monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
    resp = client.post("/api/runbook/executions", json={"name": "t-v2"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["result"] == "ok"

    page, total = list_web_approvals()
    assert total >= 1
    assert any(v.get("source") == "web" for v in page)

    from tools.runbook_exec import recent_executions
    rows = recent_executions(ehome)
    assert rows and rows[0]["runbook"] == "t-v2"
    assert rows[0]["result"] == "ok"
