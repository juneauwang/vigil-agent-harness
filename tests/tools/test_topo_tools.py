"""Ops harness topology tools — data contract tests (ops-agent-harness.md §2.2)."""

from __future__ import annotations

import json

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
    entry: "ssh jump@203.0.113.10"
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
    monkeypatch.setenv("HERMES_HOME", str(home))
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
    # entity whose detail points outside HERMES_HOME
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


def test_check_topo_requirements_gates_on_config(tmp_path, monkeypatch):
    import hermes_cli.config as hc

    home = tmp_path / "cfg"
    home.mkdir()
    (home / "config.yaml").write_text(
        "ops:\n  topology:\n    enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert topo_tools.check_topo_requirements() is True
    finally:
        hc._LOAD_CONFIG_CACHE.clear()

    (home / "config.yaml").write_text("ops:\n  topology:\n    enabled: false\n", encoding="utf-8")
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert topo_tools.check_topo_requirements() is False
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
