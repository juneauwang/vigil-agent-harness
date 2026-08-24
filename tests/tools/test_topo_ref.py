"""YAPL 主框架阶段 A：``resolve_topo_ref`` 共享库验收测试。

覆盖（任务书 §任务 2）：
  - 解析四步：精确唯一 / 上下文收敛 / 歧义报错列候选 / 无匹配拒绝；
  - kind 限定（service/host/cluster/host_group）与跨层歧义；
  - P4 resolve_target 接入回归：唯一名行为不变 + 重名不再静默（构造两个
    同名服务断言报歧义）+ runbook 执行范围 scope 收敛。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.topo_ref import resolve_topo_ref
from tools.runbook_exec import resolve_target


_TOPOLOGY = """
version: 4
environments:
- name: prod
clusters:
- name: beijing
  type: ssh
  env: prod
  host_groups:
  - name: web-tier
hosts:
- name: h1
  type: host
  env: prod
  cluster: beijing
  endpoint: 10.0.0.1
  os: Ubuntu 22.04
- name: h2
  type: host
  env: prod
  cluster: beijing
  endpoint: 10.0.0.2
  os: Ubuntu 22.04
- name: db
  type: host
  env: prod
  cluster: beijing
  endpoint: 10.0.0.3
  os: Ubuntu 22.04
"""

_H1_SERVICES = """
host: h1
services:
- name: kubelet
  type: system
  managed_by: systemd
  source: manual
- name: nginx
  type: gateway
  managed_by: docker_compose
  source: manual
  detail: entities/nginx.yaml
- name: db
  type: database
  managed_by: docker_compose
  source: manual
"""

_H2_SERVICES = """
host: h2
services:
- name: kubelet
  type: system
  managed_by: systemd
  source: manual
"""

_NGINX_ENTITY = """
name: nginx
snapshot:
  by_runtime:
    docker_compose:
      project: web
      services:
      - name: docker-nginx-1
"""


@pytest.fixture
def thome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 拓扑 fixture：两主机同名 kubelet（prod/beijing）。

    重名矩阵：kubelet × 2（h1/h2，env/cluster 相同、host 不同）；
    db 主机 + db 服务同层重名（跨 kind 歧义）；nginx 唯一（精确命中）。
    """
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text(_TOPOLOGY, encoding="utf-8")
    (home / "services" / "h1.yaml").write_text(_H1_SERVICES, encoding="utf-8")
    (home / "services" / "h2.yaml").write_text(_H2_SERVICES, encoding="utf-8")
    (home / "entities" / "nginx.yaml").write_text(_NGINX_ENTITY, encoding="utf-8")
    from tools.matrix_data import template_matrix, write_matrix
    write_matrix(template_matrix("template1"), home)
    yield home


def _topo(home: Path):
    from tools.topo_tools import load_topology
    return load_topology(home)


# ---------------------------------------------------------------------------
# 解析四步
# ---------------------------------------------------------------------------

def test_exact_unique_service(thome):
    e = resolve_topo_ref(_topo(thome), "nginx", kind="service", home=thome)
    assert e["name"] == "nginx" and e["_kind"] == "service"
    assert e["_host"] == "h1"


def test_exact_unique_host(thome):
    e = resolve_topo_ref(_topo(thome), "h1", kind="host", home=thome)
    assert e["name"] == "h1" and e["_kind"] == "host"


def test_exact_unique_cluster_and_host_group(thome):
    e = resolve_topo_ref(_topo(thome), "beijing", kind=None, home=thome)
    assert e["name"] == "beijing" and e["_kind"] == "cluster"
    hg = resolve_topo_ref(_topo(thome), "web-tier", kind="host_group", home=thome)
    assert hg["name"] == "web-tier" and hg["_kind"] == "host_group"
    assert hg.get("cluster") == "beijing" and hg.get("env") == "prod"


def test_kind_filters_candidates(thome):
    # cluster 名在 service 层不存在 → 拒绝（kind 限定生效）
    with pytest.raises(ValueError, match="不在拓扑表"):
        resolve_topo_ref(_topo(thome), "beijing", kind="service", home=thome)
    # db 主机 + db 服务同层重名：kind=service 限定后唯一
    e = resolve_topo_ref(_topo(thome), "db", kind="service", home=thome)
    assert e["_kind"] == "service" and e["_host"] == "h1"
    # kind=None 跨层歧义（host db + service db）
    with pytest.raises(ValueError, match="重名歧义"):
        resolve_topo_ref(_topo(thome), "db", kind=None, home=thome)


def test_context_convergence_by_host(thome):
    ctx = {"env": "prod", "cluster": "beijing", "host": "h2"}
    e = resolve_topo_ref(_topo(thome), "kubelet", kind="service",
                         context=ctx, home=thome)
    assert e["_host"] == "h2"


def test_context_env_cluster_only_still_ambiguous(thome):
    # 两个 kubelet 的 env/cluster 相同——env+cluster 收敛不了 → 仍报歧义
    with pytest.raises(ValueError, match="重名歧义"):
        resolve_topo_ref(_topo(thome), "kubelet", kind="service",
                         context={"env": "prod", "cluster": "beijing"},
                         home=thome)


def test_ambiguity_lists_candidates(thome):
    with pytest.raises(ValueError) as ei:
        resolve_topo_ref(_topo(thome), "kubelet", kind="service", home=thome)
    msg = str(ei.value)
    assert "重名歧义" in msg and "不静默取第一个" in msg
    assert "beijing__h1__kubelet" in msg and "beijing__h2__kubelet" in msg
    assert "env=prod" in msg


def test_no_match_rejected_with_retopology_hint(thome):
    with pytest.raises(ValueError) as ei:
        resolve_topo_ref(_topo(thome), "ghost", kind="service", home=thome)
    msg = str(ei.value)
    assert "不在拓扑表" in msg and "topo_query" in msg


def test_empty_name_rejected(thome):
    with pytest.raises(ValueError, match="值必填"):
        resolve_topo_ref(_topo(thome), "", kind="service", home=thome)


# ---------------------------------------------------------------------------
# P4 resolve_target 接入回归
# ---------------------------------------------------------------------------

def test_resolve_target_unique_unchanged(thome):
    topo = _topo(thome)
    svc = resolve_target(thome, topo, "nginx")
    assert svc["type"] == "service" and svc["host"] == "h1"
    assert svc["container"] == "docker-nginx-1"
    host = resolve_target(thome, topo, "h1")
    assert host["type"] == "host" and host["name"] == "h1"
    cluster = resolve_target(thome, topo, "beijing")
    assert cluster["type"] == "cluster"
    hg = resolve_target(thome, topo, "web-tier")
    assert hg["type"] == "host_group"


def test_resolve_target_duplicate_no_longer_silent(thome):
    # 两个同名服务：旧实现 next() 静默取第一个——现在必须报歧义
    with pytest.raises(ValueError, match="重名歧义"):
        resolve_target(thome, _topo(thome), "kubelet")


def test_resolve_target_context_converges(thome):
    e = resolve_target(thome, _topo(thome), "kubelet",
                       context={"env": "prod", "cluster": "beijing",
                                "host": "h2"})
    assert e["host"] == "h2" and e["name"] == "kubelet"


def _ok_runner():
    def runner(spec, target):
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}
    return runner


def _runbook(target: str, **scope_kw) -> dict:
    data = {
        "name": "dup-target", "title": "DupTarget", "version": 2,
        "kind": "maintenance", "env": "prod", "on_failure": "stop",
        "steps": [
            {"id": "q", "title": "q", "action": "query",
             "params": {"target": target, "pattern": "up"}},
        ],
    }
    data.update(scope_kw)
    return data


def test_execute_runbook_duplicate_target_blocks(thome, monkeypatch):
    from tools.matrix_data import load_matrix, set_level, write_matrix
    m = load_matrix(thome)
    set_level(m, "prod", "query", "execute")
    write_matrix(m, thome)
    from tools.runbook_exec import execute_runbook
    res = execute_runbook(_runbook("kubelet"), home=thome, runner=_ok_runner())
    assert res["result"] in ("failed", "blocked")
    assert "重名歧义" in res.get("error", "")


def test_execute_runbook_scope_converges(thome, monkeypatch):
    from tools.matrix_data import load_matrix, set_level, write_matrix
    m = load_matrix(thome)
    set_level(m, "prod", "query", "execute")
    write_matrix(m, thome)
    from tools.runbook_exec import execute_runbook
    # runbook 声明范围 hosts: [h2] → scope {env: prod, host: h2} → 收敛到 h2
    res = execute_runbook(_runbook("kubelet", hosts=["h2"]),
                          home=thome, runner=_ok_runner())
    assert res["result"] == "ok"
    assert res["steps"][0]["target"]["host"] == "h2"


def test_execute_runbook_host_scope_wrong_host_ambiguous(thome, monkeypatch):
    from tools.matrix_data import load_matrix, set_level, write_matrix
    m = load_matrix(thome)
    set_level(m, "prod", "query", "execute")
    write_matrix(m, thome)
    from tools.runbook_exec import execute_runbook
    # 声明范围 hosts: [db]（db 上没有 kubelet）→ 上下文收敛不了 → 报歧义
    res = execute_runbook(_runbook("kubelet", hosts=["db"]),
                          home=thome, runner=_ok_runner())
    assert res["result"] in ("failed", "blocked")
    assert "重名歧义" in res.get("error", "")
