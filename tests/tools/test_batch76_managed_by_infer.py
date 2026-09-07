"""batch76（OPS-DELTA #91）：执行器 managed_by 推断测试。

背景（dogfood 实证）：argocd-server 实体档案/L2 服务行缺 managed_by（v0.3
存量 k8s 实体只写 type=k8s-service，无 managed_by 字段）→ resolve_target
返回空 → runbook_handlers 退化为 "bare" → kubectl/docker/systemd 通道丢失。

修法：_resolve_target 在 managed_by 缺失时按 snapshot.by_runtime 键 / type
推断。优先级：显式 managed_by > by_runtime 键（topo_discovery 写 by_runtime
= {managed_by: 块}，键即发现期 managed_by）> type 映射 > 空。只补缺失，
不覆盖显式值；未知 type 不瞎猜。
"""

from __future__ import annotations

import pytest

from tools.runbook_exec import resolve_target
from tools.topo_tools import load_topology


@pytest.fixture
def infer_home(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 拓扑 fixture（v0.4 布局，服务行不带 managed_by）。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text("""
version: 4
environments:
- name: prod
clusters:
- name: beijing_cluster
  type: kubernetes
  env: prod
  host_groups: []
hosts:
- name: 203.0.113.32
  type: host
  env: prod
  cluster: beijing_cluster
  endpoint: 203.0.113.32
  os: Ubuntu 22.04
  credentials: []
""", encoding="utf-8")
    (home / "services" / "203.0.113.32.yaml").write_text("""
host: 203.0.113.32
services:
- name: argocd-server
  type: k8s-service
  source: discovered
  detail: entities/prod__203.0.113.32__argocd-server.yaml
- name: registry
  type: docker
  source: discovered
- name: app-legacy
  type: app
  source: manual
- name: app-compose
  type: gateway
  source: manual
  detail: entities/app-compose.yaml
- name: nginx
  type: gateway
  managed_by: systemd
  source: manual
""", encoding="utf-8")
    # v0.4 时代：L2 managed_by 缺失但 L3 快照带 by_runtime.kubectl → 推断 kubectl
    (home / "entities" / "app-compose.yaml").write_text("""
name: app-compose
snapshot:
  by_runtime:
    kubectl:
      namespace: default
      deployments: []
""", encoding="utf-8")
    return home


def _resolve(home, name):
    return resolve_target(home, load_topology(home), name)


def test_k8s_type_inferred_to_kubectl(infer_home):
    """无 managed_by + type=k8s-service（v0.3 存量 k8s 实体形态）→ kubectl。"""
    svc = _resolve(infer_home, "argocd-server")
    assert svc["type"] == "service"
    assert svc["managed_by"] == "kubectl"


def test_docker_type_inferred_to_docker(infer_home):
    """无 managed_by + type=docker → docker。"""
    svc = _resolve(infer_home, "registry")
    assert svc["managed_by"] == "docker"


def test_explicit_managed_by_not_overridden(infer_home):
    """显式 managed_by=systemd 不被 type 推断覆盖。"""
    svc = _resolve(infer_home, "nginx")
    assert svc["managed_by"] == "systemd"


def test_unknown_type_stays_empty(infer_home):
    """无 managed_by + 未知 type（app）→ 保持空串，不瞎猜。"""
    svc = _resolve(infer_home, "app-legacy")
    assert svc["managed_by"] == ""


def test_runtime_snapshot_inferred_before_type(infer_home):
    """L2 缺 managed_by 但 L3 快照 by_runtime 带 kubectl → 推断 kubectl。"""
    svc = _resolve(infer_home, "app-compose")
    assert svc["managed_by"] == "kubectl"
