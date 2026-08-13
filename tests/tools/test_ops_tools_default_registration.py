"""OPS-DELTA #14（修订）— topo/runbook 工具集默认注册 + 数据只挂 home 根。

验收点：
  - ``_get_platform_tools({}, 'cli')`` 返回含 topo/runbook（default profile
    开箱即用 ops 能力）；
  - 显式 ``platform_toolsets.cli`` 列表权威（[hermes-cli] 不自动加）；
  - 平台为 telegram / cron 时不加（不污染消息平台）；
  - 数据只挂在解析出的 home 根（VIGIL_HOME 指向的目录）下：
    home/topology.yaml + home/runbooks → 工具可用（topo_query / runbook_load /
    check_fn / topo memory provider 全部生效）；
  - sibling ops profile（<root>/profiles/ops）数据不再回退：home 根无数据 →
    工具不可用（OPS-DELTA #14 回退已删除，无老用户）；
  - 无数据时 topo_query 仍不可用（check_fn 数据存在性门控，行为不变）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.config as hc
from hermes_cli.tools_config import _get_platform_tools

TOPO_YAML = """\
version: 1
environments:
  - {name: prod, isolation: strict, role: prod, core_entities: [harbor]}
core_entities:
  - {name: harbor, type: registry, env: prod, detail: entities/harbor.yaml}
"""


def test_cli_default_platform_tools_include_topo_runbook():
    enabled = _get_platform_tools({}, "cli")
    assert {"topo", "runbook"} <= enabled


def test_explicit_platform_toolsets_are_authoritative():
    enabled = _get_platform_tools({"platform_toolsets": {"cli": ["hermes-cli"]}}, "cli")
    assert not ({"topo", "runbook"} & enabled)

    # 显式只选 topo → 不加 runbook
    mixed = _get_platform_tools({"platform_toolsets": {"cli": ["hermes-cli", "topo"]}}, "cli")
    assert "topo" in mixed and "runbook" not in mixed


@pytest.mark.parametrize("platform", ["telegram", "discord", "cron", "api_server"])
def test_non_cli_platforms_not_polluted(platform):
    enabled = _get_platform_tools({}, platform)
    assert not ({"topo", "runbook"} & enabled)


@pytest.fixture
def ops_root(tmp_path, monkeypatch):
    root = tmp_path / "vigil_root"
    root.mkdir()
    (root / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (root / "entities").mkdir()
    (root / "entities" / "harbor.yaml").write_text(
        "name: harbor\ntype: registry\n", encoding="utf-8"
    )
    (root / "runbooks").mkdir()
    (root / "runbooks" / "demo.yaml").write_text(
        "name: demo\ntitle: demo\nsteps:\n  - {id: s1, commands: [ls]}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(root))
    hc._LOAD_CONFIG_CACHE.clear()
    yield root
    hc._LOAD_CONFIG_CACHE.clear()


def test_home_root_data_enables_topo_runbook(ops_root, monkeypatch):
    """数据铺在 home 根 → 拓扑/runbook 工具开箱即用。"""
    from tools.runbook_tools import check_runbook_requirements, runbook_load
    from tools.topo_tools import check_topo_requirements, topo_query

    assert check_topo_requirements() is True
    assert check_runbook_requirements() is True

    # OPS-DELTA #6：无参 topo_query → 第一层总览（v0.1 兼容视图 core_entities）。
    payload = json.loads(topo_query())
    assert [e["name"] for e in payload["core_entities"]] == ["harbor"]

    loaded = json.loads(runbook_load("demo"))
    assert loaded["name"] == "demo"


def test_home_root_topo_provider_injects_block(ops_root):
    from plugins.memory.topo import TopoMemoryProvider

    provider = TopoMemoryProvider()
    assert provider.is_available() is True
    provider.initialize("sess-1", hermes_home=str(ops_root))
    block = provider.system_prompt_block()
    assert block.startswith("## TOPO") and "harbor" in block


def test_sibling_ops_profile_data_not_read(tmp_path, monkeypatch):
    """OPS-DELTA #14 回退删除：home 根无数据 + sibling ops profile 有数据 →
    工具不可用（不再回退 <root>/profiles/ops）。"""
    root = tmp_path / "vigil_root"
    ops_home = root / "profiles" / "ops"
    ops_home.mkdir(parents=True)
    (ops_home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (ops_home / "runbooks").mkdir()
    (ops_home / "runbooks" / "demo.yaml").write_text(
        "name: demo\ntitle: demo\nsteps:\n  - {id: s1, commands: [ls]}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(root))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        from tools.runbook_tools import check_runbook_requirements
        from tools.topo_tools import check_topo_requirements, topo_query

        assert check_topo_requirements() is False
        assert check_runbook_requirements() is False
        payload = json.loads(topo_query())
        assert payload.get("error")
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_no_data_topo_query_still_unavailable(tmp_path, monkeypatch):
    """无 topology.yaml → 工具仍不可用（现状）。"""
    home = tmp_path / "empty_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    from tools.topo_tools import check_topo_requirements
    assert check_topo_requirements() is False


def test_ops_topology_explicit_false_still_disables(ops_root, monkeypatch):
    """显式 ops.topology.enabled: false → 即使数据在 home 根也关闭。"""
    root = ops_root
    (root / "config.yaml").write_text(
        "ops:\n  topology:\n    enabled: false\n", encoding="utf-8"
    )
    hc._LOAD_CONFIG_CACHE.clear()
    from tools.topo_tools import check_topo_requirements
    assert check_topo_requirements() is False
