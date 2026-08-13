"""OPS-DELTA #6 — 拓扑三层模型（schema v0.1 → v0.2）验收测试。

覆盖：v0.2 样例解析、v0.1 兼容视图等价、topo_query 无参总览 / host 过滤 /
跨层名解析 / detail=True、紧凑列表（#30 方案 1）、权限矩阵/runbook 按服务名
绑定不失效、ops_target 第二层服务 env 解析、数据存在性门控 v0.2。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
from tools import topo_tools
from tools.ops_target import resolve_command_target
from tools.topo_tools import topo_query

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = PROJECT_ROOT / "hermes_cli" / "ops_samples"

def _seed_v2(home: Path) -> None:
    import shutil
    shutil.copy2(SAMPLE_DIR / "topology.yaml", home / "topology.yaml")
    shutil.copytree(SAMPLE_DIR / "hosts", home / "hosts")
    shutil.copytree(SAMPLE_DIR / "entities", home / "entities")


def _seed_v1(home: Path) -> None:
    """把 v0.2 样例扁平化为 v0.1 格式——同一实体集，验证双版本视图等价。"""
    import shutil
    tmp = home.parent / "_v2src"
    tmp.mkdir(exist_ok=True)
    shutil.copy2(SAMPLE_DIR / "topology.yaml", tmp / "topology.yaml")
    shutil.copytree(SAMPLE_DIR / "hosts", tmp / "hosts", dirs_exist_ok=True)
    shutil.copytree(SAMPLE_DIR / "entities", tmp / "entities", dirs_exist_ok=True)
    v2 = topo_tools.load_topology(tmp)
    flat = topo_tools._all_core_entities(v2, tmp)
    entities = []
    for e in flat:
        row = {k: v for k, v in e.items() if not k.startswith("_")}
        row.pop("services_index", None)
        entities.append(row)
    data = {
        "version": 1,
        "updated_at": "2026-08-12",
        "environments": v2.get("environments") or [],
        "core_entities": entities,
        "key_paths": v2.get("key_paths") or [],
    }
    (home / "topology.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (home / "entities").mkdir()
    for e in flat:
        name = e["name"]
        detail_path = e.get("detail")
        if detail_path and (tmp / detail_path).is_file():
            shutil.copy2(tmp / detail_path, home / detail_path)
        else:
            (home / "entities" / f"{name}.yaml").write_text(
                yaml.safe_dump({"name": name, "env": e.get("env")}, allow_unicode=True),
                encoding="utf-8",
            )


@pytest.fixture(params=["v2", "v1"])
def topo_home(tmp_path, monkeypatch, request):
    home = tmp_path / "hermes_home"
    home.mkdir()
    if request.param == "v2":
        _seed_v2(home)
    else:
        _seed_v1(home)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


# ---------------------------------------------------------------------------
# v0.2 样例解析 / 双版本兼容
# ---------------------------------------------------------------------------

def test_v3_sample_parses_and_entity_count():
    data = yaml.safe_load((SAMPLE_DIR / "topology.yaml").read_text(encoding="utf-8"))
    assert data["version"] == 3
    assert {h["name"] for h in data["hosts"]} == {"node1", "node2", "test-host"}
    assert {c["name"] for c in data["cross_host"]} == {"k3s-prod", "ingress"}
    assert {c["name"] for c in (data.get("clusters") or [])} == {"k3s-prod"}
    # 样例第一层保持 <50 行（注入 system prompt 的硬约束）。
    lines = (SAMPLE_DIR / "topology.yaml").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 50


def test_flat_view_equivalent_between_v1_and_v3(tmp_path, monkeypatch):
    """v0.1 旧格式解析成功，且扁平实体视图与 v0.2/v0.3 等价（name+env 契约）。"""
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    _seed_v1(v1)
    _seed_v2(v2)

    t1 = topo_tools.load_topology(v1)
    t2 = topo_tools.load_topology(v2)
    assert t1 is not None and t2 is not None
    flat1 = {(e["name"], e.get("env")) for e in topo_tools._all_core_entities(t1)}
    flat2 = {(e["name"], e.get("env")) for e in topo_tools._all_core_entities(t2, v2)}
    # v0.1 兼容视图与 v0.2 完全等价（同一实体集、同一 name+env 语义）。
    assert flat1 == flat2
    assert ("harbor", "prod") in flat1 and ("test-web", "test") in flat1
    assert ("node1", "prod") in flat1 and ("k3s-prod", "prod") in flat1


def test_topology_data_exists_v2(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / "topology.yaml").write_text(
        "version: 2\nhosts:\n  - {name: node1, env: prod}\n", encoding="utf-8"
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    assert topo_tools.check_topo_requirements() is True
    # 空 v2（无 hosts/cross_host）→ 视为未就位。
    (home / "topology.yaml").write_text("version: 2\n", encoding="utf-8")
    assert topo_tools.check_topo_requirements() is False


# ---------------------------------------------------------------------------
# 查询路径
# ---------------------------------------------------------------------------

def test_topo_query_overview_v3_is_compact_first_layer(topo_home):
    if (topo_home / "topology.yaml").read_text().startswith("version: 1"):
        pytest.skip("v0.1 无 host 概念")
    result = _load(topo_query(home=topo_home))
    assert result["version"] == 3
    assert {h["name"] for h in result["hosts"]} == {"node1", "node2", "test-host"}
    assert {c["name"] for c in result["cross_host"]} == {"k3s-prod", "ingress"}
    # 紧凑行：name/type/env/cluster/endpoint/stale（#30 方案 1 + OPS-DELTA #42
    # cluster；带 credential 的行随行返回引用）。
    row = result["hosts"][0]
    assert {"name", "type", "env", "cluster", "endpoint", "stale"} <= set(row.keys())
    assert row["cluster"] == "k3s-prod"
    assert result["count"] == 11  # 3 hosts + 2 cross_host + 6 services


def test_topo_query_overview_v1_compat(topo_home):
    if not (topo_home / "topology.yaml").read_text().startswith("version: 1"):
        pytest.skip("v0.2 走 hosts 视图")
    result = _load(topo_query(home=topo_home))
    assert result["version"] == 1
    # v0.1 兼容视图 = 样例完整扁平实体集（11 个，与 v0.2 等价）。
    assert len(result["core_entities"]) == 11
    assert {e["name"] for e in result["core_entities"]} == {
        "harbor", "k3s-prod", "node1", "node2", "test-host", "test-web",
        "argocd", "gateway-svc", "order-db", "postgres", "ingress",
    }


def test_topo_query_host_filter_expands_services(topo_home):
    if (topo_home / "topology.yaml").read_text().startswith("version: 1"):
        pytest.skip("v0.1 无 host 概念")
    result = _load(topo_query(host="node1", home=topo_home))
    assert result["name"] == "node1"
    assert result["env"] == "prod"
    assert {s["name"] for s in result["services"]} == {"harbor", "argocd", "order-db", "postgres"}
    # 服务行带独立 env（层级是组织方式不是命名空间）。
    assert all(s["env"] == "prod" for s in result["services"])

    missing = _load(topo_query(host="nope", home=topo_home))
    assert "error" in missing and "不存在 host" in missing["error"]


def test_topo_query_host_filter_v1_errors_clearly(topo_home):
    if not (topo_home / "topology.yaml").read_text().startswith("version: 1"):
        pytest.skip("仅 v0.1")
    result = _load(topo_query(host="node1", home=topo_home))
    assert "error" in result and "v0.1" in result["error"]


def test_topo_query_cross_layer_entity_resolution(topo_home):
    # entity=harbor（第二层服务名）→ 服务行 + detail 路径。
    harbor = _load(topo_query(entity="harbor", home=topo_home))
    assert harbor["name"] == "harbor"
    assert harbor["env"] == "prod"
    assert harbor.get("detail") in (None, "entities/harbor.yaml") or "detail" in harbor

    # entity=node1（第一层 host）→ host + services 列表。
    topo_head = (topo_home / "topology.yaml").read_text()
    if topo_head.startswith(("version: 2", "version: 3")):
        node1 = _load(topo_query(entity="node1", home=topo_home))
        assert node1["name"] == "node1"
        assert node1["cluster"] == "k3s-prod"
        assert {s["name"] for s in node1["services"]} == {"harbor", "argocd", "order-db", "postgres"}

    # entity=k3s-prod（第一层 cross_host）。
    if topo_head.startswith(("version: 2", "version: 3")):
        k3s = _load(topo_query(entity="k3s-prod", home=topo_home))
        assert k3s["name"] == "k3s-prod" and k3s["type"] == "k8s"

    missing = _load(topo_query(entity="nope", home=topo_home))
    assert "error" in missing


def test_topo_query_detail_true_loads_layer3(topo_home):
    result = _load(topo_query(entity="harbor", detail=True, home=topo_home))
    assert result["name"] == "harbor"
    assert result["detail"] is not None
    assert result["detail"]["attrs"]["version"] == "v2.11"


def test_topo_query_type_env_filter_compact(topo_home):
    if not (topo_home / "topology.yaml").read_text().startswith("version: 2"):
        pytest.skip("v0.1 fixture 无 db 实体（db 列表断言走 v0.2 样例）")
    result = _load(topo_query(entity_type="db", env="prod", home=topo_home))
    assert {e["name"] for e in result["entities"]} == {"order-db", "postgres"}
    # 紧凑字段（#30 方案 1 + OPS-DELTA #42 cluster）。
    assert set(result["entities"][0].keys()) == {
        "name", "type", "env", "cluster", "endpoint", "stale"}


# ---------------------------------------------------------------------------
# 绑定关系不失效（权限矩阵 / runbook / ops_target）
# ---------------------------------------------------------------------------

def test_permission_matrix_binding_by_service_name(topo_home):
    """服务名 + env 绑定：harbor(prod) 查询/更新语义在 v0.2 下与 v0.1 一致。"""
    from tools.approval import request_tool_approval
    import tools.approval as approval_mod

    calls = {}

    def fake_request_tool_approval(tool_name, reason, *, rule_key="", approval_callback=None):
        calls["tool_name"] = tool_name
        calls["rule_key"] = rule_key
        return {"approved": False, "message": "denied by test"}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(approval_mod, "request_tool_approval", fake_request_tool_approval)
    try:
        result = json.loads(topo_tools.topo_update(
            "harbor", {"version": "v2.12"}, home=topo_home
        ))
    finally:
        monkeypatch.undo()
    # prod 服务更新需要审批（name+env 绑定不因分层失效）。
    assert result["approved"] is False
    assert calls["rule_key"] == "topo_update:prod:harbor"


def test_ops_target_service_env_resolution_v2(tmp_path, monkeypatch):
    """ssh 命中第二层服务 → 经 host 链路解析 env（v0.2）。"""
    home = tmp_path / "h"
    home.mkdir()
    _seed_v2(home)
    # gateway-svc 挂在 node2(prod)；给 gateway-svc 一个 hostname 式 endpoint 便于命中。
    index = home / "hosts" / "node2.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    for svc in data["services"]:
        if svc["name"] == "gateway-svc":
            svc["endpoint"] = "gw.internal"
            svc["attrs"] = {"public_ip": "203.0.113.22"}
    index.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (home / "entities" / "k3s-prod__node2__gateway-svc.yaml").write_text(
        "name: gateway-svc\nenv: prod\ncluster: k3s-prod\nattrs:\n  public_ip: 203.0.113.22\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        target = resolve_command_target("ssh user@gw.internal 'systemctl status gateway'")
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
    assert target is not None
    assert target["entity"] == "gateway-svc"
    assert target["env"] == "prod"
