"""OPS-DELTA #1 — ops 工具默认加载，ops-init 降级为数据初始化。

全新 profile（未运行 ops-init、无 config.yaml）：
  - topology.yaml / runbooks/ 数据就位 → topo/runbook 工具出现在 schema（装上即用）；
  - 数据缺失 → 工具隐藏（banner 引导运行 vigil ops-init）；
  - 显式 ops.*.enabled: false → 始终关闭（向后兼容）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_cli.config as hc
from model_tools import get_tool_definitions
from tools.registry import invalidate_check_fn_cache
from model_tools import _clear_tool_defs_cache

TOPO_YAML = """\
version: 1
environments:
  - name: prod
    entry: "ssh user@203.0.113.10"
    isolation: strict
    role: prod
    core_entities: [harbor]
core_entities:
  - {name: harbor, type: registry, env: prod, endpoint: "203.0.113.10:30443"}
"""


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    yield
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


def _tool_names(home: Path) -> set:
    # skip_tool_search_assembly：直查 check_fn 门控后的原始工具列表，绕过
    # Tool Search auto 模式的桥接折叠（那是独立的 upstream 机制，本测试
    # 只验证 OPS-DELTA #1 的数据存在性门控）。
    tools = get_tool_definitions(
        enabled_toolsets=["topo", "runbook"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    return {t["function"]["name"] for t in tools}


def _seed_data(home: Path) -> None:
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (home / "runbooks").mkdir()
    (home / "runbooks" / "demo.yaml").write_text("name: demo\n", encoding="utf-8")


def test_fresh_profile_data_present_tools_visible(tmp_path, monkeypatch):
    """无 config.yaml（未 ops-init）但数据就位 → topo/runbook 工具默认可见。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    _seed_data(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    names = _tool_names(home)
    assert {"topo_query", "topo_update", "runbook_load", "runbook_checkpoint"} <= names


def test_fresh_profile_no_data_tools_hidden(tmp_path, monkeypatch):
    """全新空 profile（无数据、无 config）→ 工具隐藏（数据缺失门控）。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    names = _tool_names(home)
    assert "topo_query" not in names
    assert "runbook_load" not in names


def test_explicit_disabled_hides_tools_even_with_data(tmp_path, monkeypatch):
    """显式 enabled: false + 数据在 → 仍关闭（向后兼容）。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    _seed_data(home)
    (home / "config.yaml").write_text(
        "ops:\n"
        "  topology:\n    enabled: false\n"
        "  runbooks:\n    enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()

    names = _tool_names(home)
    assert "topo_query" not in names
    assert "runbook_load" not in names
