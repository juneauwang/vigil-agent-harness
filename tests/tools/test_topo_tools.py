"""Ops harness topology tools — data contract tests (ops-agent-harness.md §2.2)."""

from __future__ import annotations

import json

import yaml

import pytest

import hermes_constants
from tools import topo_tools
from tools.topo_tools import topo_query, topo_update

TOPO_YAML = """\
version: 1
updated_at: 2026-08-06
sources: [netbox, snipeit, agent]
environments:
  - name: prod
    entry: "ssh user@203.0.113.10"
    isolation: strict
    role: prod
    core_entities: [harbor, k3s-prod]
  - name: test
    entry: "ssh test-jump"
    isolation: relaxed
    role: test
core_entities:
  - name: harbor
    type: registry
    env: prod
    endpoint: 203.0.113.10:30443
    owner: your-name
    source: manual
    last_verified: 2026-08-01
    detail: entities/harbor.yaml
  - name: order-db
    type: db
    env: prod
    owner: your-name
    source: manual
key_paths:
  - [ingress, gateway-svc, order-db]
"""

HARBOR_YAML = """\
name: harbor
type: registry
env: prod
attrs:
  version: v2.11
  storage: /data/harbor
  admin: your-name
depends_on: [postgres]
depended_by: [gnomeria-dev, gnomeria-prod]
ops:
  healthcheck: "curl -s http://localhost/api/v2.0/health"
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (home / "entities").mkdir()
    (home / "entities" / "harbor.yaml").write_text(HARBOR_YAML, encoding="utf-8")
    # Point the real get_hermes_home() at the temp home via the env var (the
    # repo's standard pattern) — never patch the function object: hermes_cli
    # config binds it at import time, and a patched binding would leak across
    # tests once config.py is first imported.
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


V2_TOPO_YAML = """\
version: 2
updated_at: 2026-08-12
environments:
  - {name: prod, isolation: strict, role: prod}
hosts:
  - {name: node1, env: prod, endpoint: "203.0.113.10", services_index: hosts/node1.yaml}
  - {name: node2, env: prod, endpoint: "203.0.113.11", services_index: hosts/node2.yaml}
cross_host:
  - {name: k3s-prod, type: k8s, env: prod, detail: entities/k3s-prod.yaml}
"""

V2_NODE1_INDEX = """\
host: node1
env: prod
services:
  - {name: harbor, type: registry, env: prod, detail: entities/harbor.yaml}
  - {name: postgres, type: db, env: prod, detail: entities/postgres.yaml}
"""

V2_HARBOR = """\
name: harbor
type: registry
env: prod
attrs:
  version: v2.11
depends_on: [postgres]
"""

V2_POSTGRES = """\
name: postgres
type: db
env: prod
attrs:
  version: "16"
"""

V3_TOPO_YAML = """\
version: 3
updated_at: 2026-08-13
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: dev, isolation: relaxed, role: dev}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, description: 生产 k3s 集群, owner: your-name}
hosts:
  - {name: node1, env: prod, cluster: k3s-prod, endpoint: "203.0.113.10", services_index: hosts/node1.yaml, credential: {type: ssh_key, ref: "~/.vigil/keys/node1.pem", user: root}}
  - {name: test-host, env: test, runtime: docker, services_index: hosts/test-host.yaml}
cross_host:
  - {name: ingress, type: ingress, env: prod, cluster: k3s-prod, detail: entities/k3s-prod__ingress.yaml}
"""

V3_NODE1_INDEX = """\
host: node1
env: prod
cluster: k3s-prod
services:
  - {name: postgres, type: db, env: prod, endpoint: "203.0.113.10:5432", detail: entities/k3s-prod__node1__postgres.yaml}
  - {name: harbor, type: registry, env: prod, endpoint: "203.0.113.10:30443", detail: entities/k3s-prod__node1__harbor.yaml}
"""

V3_TEST_INDEX = """\
host: test-host
env: test
services:
  - {name: test-web, type: service, env: test, detail: entities/test__test-host__test-web.yaml}
"""

V3_POSTGRES = """\
name: postgres
type: db
env: prod
cluster: k3s-prod
ssh:
  user: dbadmin
  credential: {type: ssh_key, ref: "~/.vigil/keys/postgres.pem", user: dbadmin}
attrs:
  version: "16"
ops:
  healthcheck: "pg_isready"
"""


def _write_topo(home, files: dict):
    for rel, text in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def topo_home_v2(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    _write_topo(home, {
        "topology.yaml": V2_TOPO_YAML,
        "hosts/node1.yaml": V2_NODE1_INDEX,
        "entities/harbor.yaml": V2_HARBOR,
        "entities/postgres.yaml": V2_POSTGRES,
        "entities/k3s-prod.yaml": "name: k3s-prod\ntype: k8s\nenv: prod\n",
    })
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def topo_home_v3(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    _write_topo(home, {
        "topology.yaml": V3_TOPO_YAML,
        "hosts/node1.yaml": V3_NODE1_INDEX,
        "hosts/test-host.yaml": V3_TEST_INDEX,
        "entities/k3s-prod__node1__postgres.yaml": V3_POSTGRES,
    })
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


def _load(result: str) -> dict:
    return json.loads(result)


def test_topo_query_list_filters_by_env(topo_home):
    result = _load(topo_query(env="prod"))
    assert result["count"] == 3
    names = {e["name"] for e in result["entities"]}
    assert names == {"harbor", "order-db", "k3s-prod"}
    assert all(e["env"] == "prod" for e in result["entities"])


def test_topo_query_list_filters_by_type(topo_home):
    result = _load(topo_query(entity_type="registry"))
    assert result["count"] == 1
    assert result["entities"][0]["name"] == "harbor"


def test_topo_query_entity_missing(topo_home):
    result = _load(topo_query(entity="nope"))
    assert "error" in result
    assert "nope" in result["error"]


def test_topo_query_entity_detail_loads_layer2(topo_home):
    result = _load(topo_query(entity="harbor", detail=True))
    assert result["name"] == "harbor"
    assert result["stale"] is False  # 2026-08-01 is within _STALE_DAYS of today
    assert result["detail"]["depends_on"] == ["postgres"]
    assert result["detail"]["attrs"]["version"] == "v2.11"
    assert result["detail"]["ops"]["healthcheck"]


def test_topo_query_stale_flag(topo_home):
    topo_home.touch("topology.yaml")  # keep file fresh; nothing else needed
    # order-db is unverified → stale
    result = _load(topo_query(entity="order-db"))
    assert result["stale"] is True


def test_topo_update_test_env_writes_source_and_verified(topo_home, monkeypatch):
    # Put an entity in test env so no prod approval is triggered.
    order_db_block = (
        "  - name: order-db\n"
        "    type: db\n"
        "    env: prod\n"
        "    owner: your-name\n"
        "    source: manual\n"
    )
    topo_yaml = TOPO_YAML.replace(
        order_db_block,
        "  - name: web-test\n    type: svc\n    env: test\n    owner: your-name\n",
        1,
    )
    (topo_home / "topology.yaml").write_text(topo_yaml, encoding="utf-8")
    (topo_home / "entities" / "web-test.yaml").write_text(
        "name: web-test\nenv: test\nattrs:\n  version: v1\n", encoding="utf-8"
    )

    result = _load(topo_update("web-test", {"version": "v1.2", "status": "degraded"}))
    assert result["status"] == "updated"
    assert result["source"] == "agent"
    assert result["last_verified"]

    saved = (topo_home / "entities" / "web-test.yaml").read_text(encoding="utf-8")
    assert "version: v1.2" in saved
    assert "status: degraded" in saved
    assert "source: agent" in saved


def test_topo_update_prod_requires_approval(topo_home, monkeypatch):
    import tools.approval as approval_mod
    calls = {}

    def fake_request_tool_approval(tool_name, reason, *, rule_key="", approval_callback=None):
        calls["tool_name"] = tool_name
        calls["reason"] = reason
        calls["rule_key"] = rule_key
        return {"approved": False, "message": "denied by test"}

    monkeypatch.setattr(approval_mod, "request_tool_approval", fake_request_tool_approval)

    result = _load(topo_update("harbor", {"version": "v2.12"}))
    assert result["approved"] is False
    assert calls["tool_name"] == "topo_update"
    assert "harbor" in calls["reason"]
    assert calls["rule_key"] == "topo_update:prod:harbor"

    # File untouched after denial.
    assert "v2.12" not in (topo_home / "entities" / "harbor.yaml").read_text(encoding="utf-8")


def test_topo_update_missing_entity(topo_home):
    result = _load(topo_update("ghost", {"x": 1}))
    assert "error" in result


def test_topo_update_rejects_traversal_detail(topo_home):
    # entity whose detail points outside the data root
    topo_yaml = TOPO_YAML.replace(
        "  - name: order-db\n",
        "  - name: order-db\n  - name: evil\n    type: svc\n    env: test\n    detail: ../evil.yaml\n",
        1,
    )
    (topo_home / "topology.yaml").write_text(topo_yaml, encoding="utf-8")
    result = _load(topo_update("evil", {"x": 1}))
    assert "error" in result
    assert "越界" in result["error"]


def test_topo_query_no_topology_file(topo_home, monkeypatch):
    (topo_home / "topology.yaml").unlink()
    result = _load(topo_query())
    assert "error" in result
    assert "topology.yaml" in result["error"]


def test_check_topo_requirements_data_existence_gating(tmp_path, monkeypatch):
    """OPS-DELTA #1：topo 工具默认按数据存在性可用，enabled 降级为显式覆盖。"""
    import hermes_cli.config as hc

    home = tmp_path / "cfg"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))

    def _check():
        hc._LOAD_CONFIG_CACHE.clear()
        try:
            return topo_tools.check_topo_requirements()
        finally:
            hc._LOAD_CONFIG_CACHE.clear()

    # 无 config 且无 topology.yaml → 不可用（数据缺失）
    assert _check() is False

    # 无 config（默认加载）但 topology.yaml 就位 → 可用
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    assert _check() is True

    # topology.yaml 存在但空（无 core_entities）→ 视为未就位
    (home / "topology.yaml").write_text("version: 1\n", encoding="utf-8")
    assert _check() is False

    # 显式 enabled: false → 始终关闭（向后兼容）
    (home / "config.yaml").write_text(
        "ops:\n  topology:\n    enabled: false\n", encoding="utf-8"
    )
    assert _check() is False

    # 显式 enabled: true 但数据缺失 → 仍不可用（工具无数据只会报错）
    (home / "topology.yaml").unlink()
    (home / "config.yaml").write_text(
        "ops:\n  topology:\n    enabled: true\n", encoding="utf-8"
    )
    assert _check() is False


def test_topo_query_cluster_filter_v3(topo_home_v3):
    """cluster= 过滤只返回该 cluster 的 host/服务/cross_host 行。"""
    result = _load(topo_query(cluster="k3s-prod"))
    names = {e["name"] for e in result["entities"]}
    assert names == {"node1", "postgres", "harbor", "ingress"}
    assert all((e.get("cluster") or "default") == "k3s-prod" for e in result["entities"])


def test_topo_query_cluster_default_for_host_without_cluster(topo_home_v3):
    """host 无 cluster → 显示 default（不写回文件）。"""
    result = _load(topo_query(cluster="default"))
    names = {e["name"] for e in result["entities"]}
    assert names == {"test-host", "test-web"}
    host_row = _load(topo_query(host="test-host"))
    assert host_row["cluster"] == "default"
    topo = yaml.safe_load((topo_home_v3 / "topology.yaml").read_text(encoding="utf-8"))
    assert "cluster" not in next(h for h in topo["hosts"] if h["name"] == "test-host")


def test_topo_query_overview_includes_clusters_v3(topo_home_v3):
    result = _load(topo_query())
    assert result["version"] == 3
    assert {c["name"] for c in result["clusters"]} == {"k3s-prod"}
    assert {h["name"] for h in result["hosts"]} == {"node1", "test-host"}


def test_topo_query_credential_reference_returned_with_port_default(topo_home_v3):
    """host credential 引用随 topo_query 返回；port 缺省 22。"""
    host_row = _load(topo_query(host="node1"))
    assert host_row["credential"]["type"] == "ssh_key"
    assert host_row["credential"]["ref"] == "~/.vigil/keys/node1.pem"
    assert host_row["credential"]["user"] == "root"
    assert host_row["credential"]["port"] == 22
    # 总览紧凑行同样带 credential（LLM 直接拿"怎么连"）。
    overview = _load(topo_query(cluster="k3s-prod"))
    node1_row = next(e for e in overview["entities"] if e["name"] == "node1")
    assert node1_row["credential"]["port"] == 22


def test_topo_query_entity_ssh_section_overrides_host_level(topo_home_v3):
    """实体 ssh 段（L3 detail）覆盖 host 级默认连接信息。"""
    result = _load(topo_query(entity="postgres", detail=True))
    assert result["detail"]["ssh"]["user"] == "dbadmin"
    assert result["detail"]["ssh"]["credential"]["type"] == "ssh_key"
    assert result["detail"]["ssh"]["credential"]["ref"] == "~/.vigil/keys/postgres.pem"


def test_v02_old_topology_loads_bytes_and_mtime_unchanged(tmp_path, monkeypatch):
    """读取兼容硬要求：v0.2 老文件完整加载，原文件字节/mtime 不变。"""
    home = tmp_path / "vigil_home"
    home.mkdir()
    files = {
        "topology.yaml": V2_TOPO_YAML,
        "hosts/node1.yaml": V2_NODE1_INDEX,
        "entities/harbor.yaml": V2_HARBOR,
        "entities/postgres.yaml": V2_POSTGRES,
        "entities/k3s-prod.yaml": "name: k3s-prod\ntype: k8s\nenv: prod\n",
    }
    _write_topo(home, files)
    before = {rel: (home / rel).read_bytes() for rel in files}
    mtimes = {rel: (home / rel).stat().st_mtime_ns for rel in files}
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        host_row = _load(topo_query(host="node1"))
        assert {s["name"] for s in host_row["services"]} == {"harbor", "postgres"}
        assert host_row["cluster"] == "default"   # v0.2 host 无 cluster → 补 default
        detail = _load(topo_query(entity="harbor", detail=True))
        assert detail["detail"]["depends_on"] == ["postgres"]
        overview = _load(topo_query())
        assert overview["version"] == 2
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()
    after = {rel: (home / rel).read_bytes() for rel in files}
    assert after == before
    assert all(mtimes[rel] == (home / rel).stat().st_mtime_ns for rel in files)


def test_v03_topology_loads_cluster_shape_without_version_field(tmp_path, monkeypatch):
    """version 缺失时 shape 检测：hosts/cross_host/clusters 都在 → 分层结构。"""
    home = tmp_path / "vigil_home"
    home.mkdir()
    no_version = V3_TOPO_YAML.replace("version: 3\n", "")
    _write_topo(home, {
        "topology.yaml": no_version,
        "hosts/node1.yaml": V3_NODE1_INDEX,
        "entities/k3s-prod__node1__postgres.yaml": V3_POSTGRES,
    })
    monkeypatch.setenv("VIGIL_HOME", str(home))
    import hermes_cli.config as _hc
    _hc._LOAD_CONFIG_CACHE.clear()
    try:
        host_row = _load(topo_query(host="node1"))
        assert {s["name"] for s in host_row["services"]} == {"postgres", "harbor"}
        assert host_row["cluster"] == "k3s-prod"
    finally:
        _hc._LOAD_CONFIG_CACHE.clear()


def test_topo_discover_tool_registered_in_topo_toolset():
    from tools.registry import registry

    entry = registry.get_entry("topo_discover")
    assert entry is not None
    assert entry.toolset == "topo"
    assert "topo_discover" in registry.get_tool_names_for_toolset("topo")


def test_topo_discover_handler_returns_fragment_without_writing(tmp_path, monkeypatch):
    fragment = {
        "version": 2,
        "source": "discovered",
        "needs_review": True,
        "host": {"name": "203.0.113.20", "env": "prod"},
        "services": [],
        "details": {},
        "probes": {},
    }
    monkeypatch.setattr(topo_tools, "discover_host", lambda *a, **kw: fragment)
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / "hermes_home"))

    result = json.loads(topo_tools._discover_handler(
        {"host": "203.0.113.20", "env": "prod", "dry_run": True}
    ))

    # 发现片段原样透传（不落盘），另附 _guide 下一步引导（批次十二 C3）。
    for key, value in fragment.items():
        assert result[key] == value
    assert result["_guide"].startswith("发现完成：0 个实体")
    assert "topo_query" in result["_guide"] and "topo_update" in result["_guide"]
    assert not (tmp_path / "hermes_home" / "topology.yaml").exists()
    assert not (tmp_path / "hermes_home" / "hosts").exists()


def test_topo_discover_handler_returns_guide_with_next_steps(topo_home_v3, monkeypatch):
    """批次十二 C3：topo_discover 工具返回 JSON + _guide 下一步引导（LLM 不再绕圈）。"""
    import json as _json
    from tools import topo_tools as tt_mod
    from tools.topo_tools import _discover_handler, _TOPO_DISCOVER_SCHEMA

    def fake_discover(host, env, creds, *, cluster="", runner=None, skip_unidentified=False):
        return {
            "version": 3,
            "source": "discovered",
            "last_verified": "2026-08-13",
            "needs_review": True,
            "host": {"name": host, "env": env, "cluster": cluster or "default"},
            "services": [{"name": "postgres", "type": "db", "env": env}],
            "details": {},
            "probes": {},
        }

    monkeypatch.setattr(tt_mod, "discover_host", fake_discover)
    result = _json.loads(_discover_handler({"host": "node1", "env": "prod", "cluster": "k3s-prod"}))
    assert result["_guide"].startswith("发现完成：1 个实体")
    assert "topo_query" in result["_guide"] and "topo_update" in result["_guide"]
    assert "needs_review=false" in result["_guide"]
    # schema description 同步确认指引（发现后需确认，用 topo_update 置 false）。
    assert "topo_update" in _TOPO_DISCOVER_SCHEMA["description"]
    assert "needs_review=false" in _TOPO_DISCOVER_SCHEMA["description"]
    assert "未经确认不参与权限判定" in _TOPO_DISCOVER_SCHEMA["description"]


# =========================================================================
# 批次三十七 §Y — 实体可选 ports 字段（schema v0.3，load 校验）
# =========================================================================

class TestNormalizePorts:
    def test_valid_list_normalized(self):
        assert topo_tools._normalize_ports([9090, 443, "8080"]) == [9090, 443, 8080]

    def test_missing_and_empty_are_none(self):
        assert topo_tools._normalize_ports(None) is None
        assert topo_tools._normalize_ports([]) is None
        assert topo_tools._normalize_ports("") is None

    def test_invalid_shapes_dropped_never_raise(self):
        assert topo_tools._normalize_ports("9090") is None
        assert topo_tools._normalize_ports({"ports": [1]}) is None
        # 越界 / 非整数 / 布尔逐项丢弃，合法项保留。
        assert topo_tools._normalize_ports([0, 9090, 70000, "x", True]) == [9090]

    def test_float_ports_coerced_when_integral(self):
        assert topo_tools._normalize_ports([9090.0]) == [9090]


class TestPortsFieldPropagation:
    """ports 从实体文件/服务行经 load 校验后进入查询与 build_view 数据。"""

    def test_entity_detail_ports_carried_through_query(self, topo_home_v3, monkeypatch, tmp_path):
        # 给 v3 的 postgres 实体文件补 ports → detail 查询带出校验后的列表。
        from tools.topo_tools import _load_entity_file, topo_query
        entity_file = topo_home_v3 / "entities" / "k3s-prod__node1__postgres.yaml"
        entity_file.write_text(
            entity_file.read_text(encoding="utf-8")
            + "\nports: [5432, 15432]\n",
            encoding="utf-8",
        )
        result = _load(topo_query(entity="postgres", detail=True))
        assert result["detail"]["ports"] == [5432, 15432]

    def test_invalid_ports_never_break_loading(self, topo_home_v3):
        from tools.topo_tools import load_topology, _load_entity_file
        entity_file = topo_home_v3 / "entities" / "k3s-prod__node1__postgres.yaml"
        entity_file.write_text(
            entity_file.read_text(encoding="utf-8")
            + "\nports: [not-a-port, 5432]\n",
            encoding="utf-8",
        )
        topo = load_topology(topo_home_v3)
        row = {"name": "postgres", "cluster": "k3s-prod",
               "detail": "entities/k3s-prod__node1__postgres.yaml"}
        detail = _load_entity_file(topo_home_v3, row)
        assert detail["ports"] == [5432]
