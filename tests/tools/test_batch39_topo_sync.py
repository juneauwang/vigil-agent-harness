"""批三十九：拓扑三层写同步 + 查询一致性 + 实体文件名双后缀（§AV 场景验收）。

v0.4（YAPL P1）语义更新：
  - needs_review/managed_by/endpoint 等是 L2 服务行字段（services/<host>.yaml），
    topo_update 直接写 L2 + 同步 L3 档案的 source/last_verified；L3 档案顶层
    白名单 = version/notes/checks/status/healthcheck/owner/env（无 needs_review）；
  - status 是 L3 顶层字段（v0.4 快照语义），不再同步进 L2 服务行；
  - 无 L2 层的实体（v0.1 扁平 / cross_host）L2 字段直接落 L3 顶层（兼容）；
  - v0.3 存量 L3 档案带 needs_review 时，查询仍以 L3 为权威（读兼容）。

覆盖：
  - 任务 1：topo_update 写 L2 后同步一致（needs_review 走 L2 白名单）、
    status 写 L3、索引写失败返回带 warning 且 L3 已写入不受影响；
  - 任务 2：查询一致性兜底——needs_review 以 L2 为权威（v0.4）/以 L3 为权威
    （v0.3 存量兼容），entity= 与 host= 两条路径同款；
  - 任务 3：_entity_filename 剥 .yaml/.yml 后缀（杜绝 *.yaml.yaml 双后缀）、
    {base}-{base} 重复名去重、write_discovery fallback 干净、存量双后缀文件
    查询兼容；
  - 任务 4：共享 sync_l2_index_row 帮助函数——缺行补建、越界拒绝、topo_update
    写失败告警接线、topo_status_sync confirm 经 topo_update 收敛到同一入口。
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
import yaml

from tools.topo_discovery import (
    ProbeResult,
    _entity_filename,
    sync_l2_index_row,
    write_discovery,
)
from tools.topo_tools import topo_query, topo_status_sync, topo_update

V4_TOPO = """\
version: 4
updated_at: 2026-08-17
environments:
  - {name: local, isolation: relaxed, role: local}
hosts:
  - {name: desktop-on88k3a, type: host, env: local, cluster: default, endpoint: "192.168.1.5", role: [docker-host], runtime: [docker], os: "", source: manual, last_verified: "2026-08-17"}
"""

SERVICES_INDEX = """\
host: desktop-on88k3a
updated_at: 2026-08-17
services:
  - {name: dsl-review, type: app, managed_by: docker_compose, detail: entities/local__desktop-on88k3a__dsl-review.yaml, endpoint: "192.168.1.5:8080", extra_ports: [], log_paths: [], depends_on: [], needs_review: true, source: discovered, last_verified: "2026-08-01"}
"""

ENTITY_YAML = """\
name: dsl-review
detail: entities/local__desktop-on88k3a__dsl-review.yaml
version: latest
updated_at: 2026-08-17
checks: []
snapshot:
  captured_at: "2026-08-17T00:00:00+00:00"
  source: discovered
  common: {version: latest, config_dir: "", log_dir: "", data_dir: "", mode: single}
  by_type: {}
  by_runtime:
    docker_compose:
      project: dsl-review
      workdir: ""
      services:
        - {name: app-container, state: running}
notes: ""
"""

# v0.3 存量档案（读兼容测试用：L3 顶层带 needs_review/status/attrs）。
LEGACY_ENTITY_YAML = """\
name: dsl-review
type: service
env: local
cluster: default
status: running
needs_review: true
source: discovered
last_verified: "2026-08-01"
attrs:
  image: dsl-review:latest
  container: app-container
"""


def _write(home, files: dict):
    for rel, text in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def sync_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    _write(home, {
        "topology.yaml": V4_TOPO,
        "services/desktop-on88k3a.yaml": SERVICES_INDEX,
        "entities/local__desktop-on88k3a__dsl-review.yaml": ENTITY_YAML,
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


def _l3(sync_home) -> dict:
    return yaml.safe_load(
        (sync_home / "entities" / "local__desktop-on88k3a__dsl-review.yaml").read_text(encoding="utf-8")
    )


def _l2(sync_home) -> dict:
    return yaml.safe_load(
        (sync_home / "services" / "desktop-on88k3a.yaml").read_text(encoding="utf-8")
    )["services"][0]


class TestTopoUpdateThreeLayerSync:
    def test_needs_review_l2_synced_and_query_consistent(self, sync_home):
        result = _load(topo_update("dsl-review", {"needs_review": False}, home=sync_home))
        assert result["status"] == "updated"
        # 审计注明已同步 L2 索引。
        assert result["l2_index_synced"] == "desktop-on88k3a"
        # v0.4：needs_review 是 L2 服务行字段，L3 档案顶层不落（白名单外）。
        l3 = _l3(sync_home)
        assert "needs_review" not in l3
        # L2 索引行同步（needs_review/source/last_verified）。
        row = _l2(sync_home)
        assert row["needs_review"] is False
        assert row["source"] == "agent"
        assert row["last_verified"] == _dt.date.today().isoformat()
        # 查询一致：entity= 与 host= 两条路径同款（L3 无 needs_review → L2 权威）。
        query = _load(topo_query(entity="dsl-review", home=sync_home))
        assert query["needs_review"] is False
        query_host = _load(topo_query(host="desktop-on88k3a", home=sync_home))
        svc = next(s for s in query_host["services"] if s["name"] == "dsl-review")
        assert svc["needs_review"] is False

    def test_needs_review_l2_false_and_true_consistent(self, sync_home):
        for value in (False, True):
            topo_update("dsl-review", {"needs_review": value}, home=sync_home)
            assert _l2(sync_home)["needs_review"] is value
            query = _load(topo_query(entity="dsl-review", home=sync_home))
            assert query["needs_review"] is value

    def test_status_update_writes_l3_not_l2(self, sync_home):
        result = _load(topo_update("dsl-review", {"status": "stopped"}, home=sync_home))
        assert result["status"] == "updated"
        # v0.4：status 是 L3 顶层字段（快照语义），不进 L2 服务行。
        assert _l3(sync_home)["status"] == "stopped"
        assert "status" not in _l2(sync_home)
        query = _load(topo_query(entity="dsl-review", home=sync_home))
        assert query["status"] == "stopped"

    def test_l2_sync_failure_warns_and_keeps_l3(self, sync_home, monkeypatch):
        monkeypatch.setattr(
            "tools.topo_tools.sync_l2_index_row",
            lambda *a, **k: "L2 索引同步失败：磁盘只读",
        )
        result = _load(topo_update("dsl-review", {"needs_review": False}, home=sync_home))
        assert result["status"] == "updated"
        assert result["l2_index_warning"] == "L2 索引同步失败：磁盘只读"
        # L3 已写入（source/last_verified）不受影响。
        assert _l3(sync_home)["source"] == "agent"

    def test_v1_flat_entity_no_l2_sync(self, tmp_path, monkeypatch):
        # v0.1 扁平结构没有 L2 索引层——不触发同步，返回也不带 l2 字段；
        # needs_review 直接落 L3 顶层（无 L2 层的唯一存储面）。
        home = tmp_path / "vigil_home"
        home.mkdir()
        _write(home, {
            "topology.yaml": (
                "version: 1\n"
                "environments:\n"
                "  - {name: test, isolation: relaxed, role: test}\n"
                "core_entities:\n"
                "  - {name: web-test, type: svc, env: test}\n"
            ),
            "entities/web-test.yaml": "name: web-test\nenv: test\nneeds_review: true\n",
        })
        monkeypatch.setenv("VIGIL_HOME", str(home))
        import hermes_cli.config as _hc
        _hc._LOAD_CONFIG_CACHE.clear()
        try:
            result = _load(topo_update("web-test", {"needs_review": False}, home=home))
        finally:
            _hc._LOAD_CONFIG_CACHE.clear()
        assert result["status"] == "updated"
        assert "l2_index_synced" not in result
        assert "l2_index_warning" not in result
        l3 = yaml.safe_load((home / "entities" / "web-test.yaml").read_text(encoding="utf-8"))
        assert l3["needs_review"] is False


class TestQueryReadFallback:
    def test_l2_value_authoritative_when_l3_lacks_field(self, sync_home):
        # v0.4：L3 档案无 needs_review（白名单外）→ 查询用 L2 行值。
        query = _load(topo_query(entity="dsl-review", home=sync_home))
        assert query["needs_review"] is True
        query_host = _load(topo_query(host="desktop-on88k3a", home=sync_home))
        svc = next(s for s in query_host["services"] if s["name"] == "dsl-review")
        assert svc["needs_review"] is True

    def test_v3_legacy_l3_needs_review_authoritative(self, sync_home):
        # v0.3 存量档案（L3 顶层带 needs_review）：查询仍以 L3 为权威（读兼容）。
        l3_path = sync_home / "entities" / "local__desktop-on88k3a__dsl-review.yaml"
        l3_path.write_text(
            LEGACY_ENTITY_YAML.replace("needs_review: true", "needs_review: false"),
            encoding="utf-8",
        )
        query = _load(topo_query(entity="dsl-review", home=sync_home))
        assert query["needs_review"] is False
        query_host = _load(topo_query(host="desktop-on88k3a", home=sync_home))
        svc = next(s for s in query_host["services"] if s["name"] == "dsl-review")
        assert svc["needs_review"] is False


class TestEntityFilename:
    def test_no_double_suffix_for_ext_names(self):
        assert (
            _entity_filename("local", "desktop-on88k3a", "dsl-review.yaml")
            == "entities/local__desktop-on88k3a__dsl-review.yaml"
        )
        assert (
            _entity_filename("local", "desktop-on88k3a", "dsl-review.yml")
            == "entities/local__desktop-on88k3a__dsl-review.yaml"
        )

    def test_repeated_name_deduped(self):
        assert (
            _entity_filename("local", "desktop-on88k3a", "dsl-review-dsl-review")
            == "entities/local__desktop-on88k3a__dsl-review.yaml"
        )
        assert (
            _entity_filename("local", "desktop-on88k3a", "dsl-review-dsl-review.yaml")
            == "entities/local__desktop-on88k3a__dsl-review.yaml"
        )

    def test_write_discovery_fallback_is_clean(self, tmp_path):
        home = tmp_path / "home"
        discovery = {
            "host": {"name": "h1", "env": "local", "cluster": "default"},
            "services": [],
            "details": {
                "foo.yaml": {"name": "foo.yaml", "env": "local", "cluster": "default", "needs_review": True},
            },
        }
        out = write_discovery(home, discovery)
        assert "entities/foo.yaml" in out["written"]
        assert not any(str(p).endswith(".yaml.yaml") for p in out["written"])


class TestLegacyDoubleSuffixCompat:
    def test_legacy_double_suffix_file_queryable(self, sync_home):
        # 磁盘上只有存量 *.yaml.yaml，L2 行 detail 指向单后缀路径 → 查询仍命中。
        (sync_home / "entities" / "local__desktop-on88k3a__dsl-review.yaml").rename(
            sync_home / "entities" / "local__desktop-on88k3a__dsl-review.yaml.yaml"
        )
        query = _load(topo_query(entity="dsl-review", detail=True, home=sync_home))
        assert query["name"] == "dsl-review"
        assert query["detail"]["name"] == "dsl-review"
        assert query["detail"]["snapshot"]["by_runtime"]["docker_compose"]["project"] == "dsl-review"


class TestSharedL2SyncHelper:
    def test_rebuilds_missing_row(self, tmp_path):
        home = tmp_path / "home"
        _write(home, {"services/h1.yaml": "host: h1\nservices: []\n"})
        warning = sync_l2_index_row(
            home,
            host_name="h1",
            entity={"name": "svc", "type": "app", "detail": "entities/x.yaml", "cluster": "default"},
            fields={"needs_review": False, "source": "agent"},
        )
        assert warning == ""
        data = yaml.safe_load((home / "services" / "h1.yaml").read_text(encoding="utf-8"))
        row = data["services"][0]
        assert row["name"] == "svc"
        assert row["detail"] == "entities/x.yaml"
        assert row["needs_review"] is False
        assert row["source"] == "agent"

    def test_rejects_traversal_services_index(self, tmp_path):
        warning = sync_l2_index_row(
            tmp_path,
            host_name="h1",
            entity={"name": "svc"},
            fields={},
            services_index="../evil.yaml",
        )
        assert "越界" in warning

    def test_status_sync_confirm_converges_to_shared_entry(self, sync_home):
        class Runner:
            def __call__(self, cmd):
                return ProbeResult('{"Names":"/app-container","State":"exited","Labels":""}')

        result = _load(topo_status_sync(confirm=True, home=sync_home, runner=Runner()))
        assert result["changed"] == 1
        # topo_update（共享入口）写 status 到 L3 顶层（v0.4 快照语义）；
        # L2 索引行同步 source/last_verified，status 不进 L2 服务行。
        assert _l3(sync_home)["status"] == "stopped"
        assert "status" not in _l2(sync_home)
        assert _l2(sync_home)["source"] == "agent"
