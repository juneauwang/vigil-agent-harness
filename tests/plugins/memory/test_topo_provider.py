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
