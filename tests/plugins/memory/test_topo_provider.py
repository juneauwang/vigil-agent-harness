"""Topo memory provider — zero-intrusion TOPO injection (ops-agent-harness.md §7 A)."""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from plugins.memory.topo import TopoMemoryProvider, render_topo_block

TOPO_YAML = """\
version: 1
updated_at: 2026-08-06
sources: [netbox, snipeit, agent]
environments:
  - name: prod
    entry: "ssh user@203.0.113.10"
    isolation: strict
    role: prod
    core_entities: [harbor]
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
key_paths:
  - [ingress, gateway-svc, order-db]
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _enable(monkeypatch, enabled=True):
    from hermes_constants import get_hermes_home
    home = __import__("pathlib").Path(get_hermes_home())
    (home / "config.yaml").write_text(
        f"ops:\n  topology:\n    enabled: {str(enabled).lower()}\n", encoding="utf-8"
    )
    hc._LOAD_CONFIG_CACHE.clear()


def test_provider_name():
    assert TopoMemoryProvider().name == "topo"


def test_is_available_gates_on_config(topo_home, monkeypatch):
    _enable(monkeypatch, enabled=True)
    assert TopoMemoryProvider().is_available() is True
    _enable(monkeypatch, enabled=False)
    assert TopoMemoryProvider().is_available() is False


def test_is_available_defaults_to_data_existence(topo_home, monkeypatch):
    """缺省 ops.topology.enabled → 按数据存在性（OPS-DELTA #14：默认 topo）。"""
    # 有 topology.yaml、无 config → available
    assert TopoMemoryProvider().is_available() is True
    # 数据不在 → 不可用（provider 静默，不注入 TOPO 段）
    (topo_home / "topology.yaml").unlink()
    assert TopoMemoryProvider().is_available() is False


def test_system_prompt_block_renders_topo_section(topo_home, monkeypatch):
    _enable(monkeypatch, enabled=True)
    provider = TopoMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(topo_home), platform="cli")
    block = provider.system_prompt_block()
    assert block.startswith("## TOPO — 平台拓扑总览")
    assert "harbor" in block
    assert "prod" in block and "test" in block
    assert "ssh user@203.0.113.10" in block
    assert "ingress → gateway-svc → order-db" in block
    # §4 C 行为约束随 TOPO 段注入
    assert "跨环境操作默认拒绝" in block


def test_system_prompt_block_empty_without_topology_file(topo_home, monkeypatch):
    _enable(monkeypatch, enabled=True)
    (topo_home / "topology.yaml").unlink()
    provider = TopoMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(topo_home), platform="cli")
    assert provider.system_prompt_block() == ""


def test_system_prompt_block_empty_before_initialize(topo_home, monkeypatch):
    _enable(monkeypatch, enabled=True)
    assert TopoMemoryProvider().system_prompt_block() == ""


def test_provider_exposes_no_tools(topo_home, monkeypatch):
    _enable(monkeypatch, enabled=True)
    # 工具走 registry topo toolset，避免双注册
    assert TopoMemoryProvider().get_tool_schemas() == []


def test_render_topo_block_max_lines(topo_home, monkeypatch):
    _enable(monkeypatch, enabled=True)
    block = render_topo_block(topo_home, max_lines=6)
    assert len(block.splitlines()) <= 6


def test_plugin_discoverable():
    from plugins.memory import load_memory_provider
    provider = load_memory_provider("topo")
    assert provider is not None
    assert provider.name == "topo"


def test_v2_render_injects_only_first_layer(tmp_path, monkeypatch):
    """OPS-DELTA #6：v0.2 下 TOPO 段只注入第一层（hosts+cross_host），
    服务在第二层、不进 system prompt（token 成本恒定）。"""
    import shutil
    from pathlib import Path as _P
    sample = _P(__file__).resolve().parents[3] / "hermes_cli" / "ops_samples"
    home = tmp_path / "hermes_home"
    home.mkdir()
    shutil.copy2(sample / "topology.yaml", home / "topology.yaml")
    shutil.copytree(sample / "hosts", home / "hosts")
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    provider = TopoMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(home), platform="cli")
    block = provider.system_prompt_block()

    # 第一层总览：主机 + 跨主机实体 + 关键链路。
    assert "node1" in block and "test-host" in block
    assert "k3s-prod" in block and "ingress" in block
    assert "ingress → gateway-svc → order-db" in block
    assert "runtime=k3s" in block and "runtime=docker" in block
    # 第二层服务不进注入块。
    assert "harbor" not in block
    assert "postgres" not in block
    hc._LOAD_CONFIG_CACHE.clear()
