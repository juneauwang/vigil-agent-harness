"""OPS-DELTA #38 — 首装即完整 ops harness：default profile 首装自带 ops 配置与样例。

覆盖批次七验收：
  - 任务 1：DEFAULT_CONFIG 预置 ops 段（env=test 安全默认）、platform_toolsets.cli
    含 topo/runbook、tool_search 默认 off、memory.provider: topo 保持；
  - 任务 2：ensure_hermes_home 首装铺样例（topology.yaml + hosts/ + entities/ +
    runbooks/）到数据根，幂等、老数据根不碰；
  - 任务 4：default config 含 ops 段 → banner GATES matrix ON；
  - 任务 5：SOUL persona 与 ops 段绑定（有 ops → ops persona；无 ops → 通用
    降级 persona；用户自定义 SOUL.md 永不覆盖）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import hermes_cli.banner as banner
import hermes_cli.config as hc
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.default_soul import (
    DEFAULT_SOUL_MD,
    DEFAULT_SOUL_MD_GENERIC,
    _LEGACY_TEMPLATE_SOULS,
    default_soul_for_config,
)
from hermes_cli.tools_config import _get_platform_tools


@pytest.fixture(autouse=True)
def _clear_caches():
    hc._LOAD_CONFIG_CACHE.clear()
    hc._VIGIL_HOME_ENSURED.clear()
    banner._banner_state_cache = None
    yield
    hc._LOAD_CONFIG_CACHE.clear()
    hc._VIGIL_HOME_ENSURED.clear()
    banner._banner_state_cache = None


def _fresh_home(tmp_path: Path, monkeypatch, name: str = "vigil-fresh") -> Path:
    """Point VIGIL_HOME at a not-yet-existing data root (true first install)."""
    home = tmp_path / name
    monkeypatch.setenv("VIGIL_HOME", str(home))
    return home


class TestDefaultConfigOps:
    def test_default_config_has_ops_section_with_safe_defaults(self):
        ops = DEFAULT_CONFIG["ops"]
        assert [e["name"] for e in ops["environments"]] == ["local", "test", "dev", "prod"]
        assert ops["topology"]["enabled"] is True
        assert ops["runbooks"]["enabled"] is True
        assert ops["watch"]["enabled"] is False
        assert ops["prometheus"]["endpoint"] == ""
        assert ops["prometheus"]["alertmanager"] == ""
        assert ops["prometheus"]["vault_path"] == ""
        perms = ops["permissions"]
        assert perms["enabled"] is True
        assert perms["env"] == "test"
        assert perms["role"] == "test"

    def test_default_platform_toolsets_cli_has_topo_runbook(self):
        assert DEFAULT_CONFIG["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook", "matrix"]

    def test_default_tool_search_off(self):
        assert DEFAULT_CONFIG["tools"]["tool_search"]["enabled"] == "off"

    def test_default_memory_provider_topo(self):
        assert DEFAULT_CONFIG["memory"]["provider"] == "topo"

    def test_get_platform_tools_empty_dict_includes_topo_runbook(self):
        enabled = _get_platform_tools({"platform_toolsets": {}}, "cli")
        assert "topo" in enabled
        assert "runbook" in enabled

    def test_fresh_load_config_carries_ops_and_platform_toolsets(self, tmp_path, monkeypatch):
        _fresh_home(tmp_path, monkeypatch)
        cfg = hc.load_config()
        assert cfg["ops"]["permissions"]["env"] == "test"
        assert cfg["ops"]["permissions"]["role"] == "test"
        assert cfg["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook", "matrix"]
        assert cfg["tools"]["tool_search"]["enabled"] == "off"

    def test_user_config_missing_ops_merges_defaults_without_error(self, tmp_path, monkeypatch):
        """升级路径：已有 config.yaml 缺 ops 段 → 默认值补齐不报错。"""
        home = _fresh_home(tmp_path, monkeypatch)
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(yaml.safe_dump({"model": "deepseek-v3"}), encoding="utf-8")
        cfg = hc.load_config()
        assert cfg["model"] == "deepseek-v3"
        assert cfg["ops"]["permissions"]["env"] == "test"
        assert cfg["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook", "matrix"]


class TestFirstRunSeeding:
    def test_fresh_home_seeds_ops_samples(self, tmp_path, monkeypatch):
        home = _fresh_home(tmp_path, monkeypatch)
        hc.ensure_hermes_home()
        assert (home / "topology.yaml").is_file()
        assert (home / "runbooks").is_dir() and any((home / "runbooks").glob("*.yaml"))
        assert (home / "services").is_dir() and any((home / "services").glob("*.yaml"))
        assert (home / "entities").is_dir() and any((home / "entities").glob("*.yaml"))
        assert (home / "hardware").is_dir() and any((home / "hardware").glob("*.yaml"))

    def test_seeding_idempotent_and_does_not_overwrite(self, tmp_path, monkeypatch):
        home = _fresh_home(tmp_path, monkeypatch)
        hc.ensure_hermes_home()
        topo = home / "topology.yaml"
        before = topo.read_bytes()
        mtime = topo.stat().st_mtime_ns
        hc._VIGIL_HOME_ENSURED.clear()
        hc.ensure_hermes_home()
        assert topo.read_bytes() == before
        assert topo.stat().st_mtime_ns == mtime

    def test_existing_root_untouched_no_new_sample_files(self, tmp_path, monkeypatch):
        """老数据根（含 profiles/ops）→ 不铺样例、不覆盖既有文件。"""
        home = tmp_path / "old-root"
        (home / "profiles" / "ops").mkdir(parents=True)
        (home / "config.yaml").write_text("model: old\n", encoding="utf-8")
        topo = home / "profiles" / "ops" / "topology.yaml"
        topo.write_text("old: data\n", encoding="utf-8")
        soul = home / "SOUL.md"
        soul.write_text("my custom soul\n", encoding="utf-8")
        mtimes = {p: p.stat().st_mtime_ns for p in (home / "config.yaml", topo, soul)}
        monkeypatch.setenv("VIGIL_HOME", str(home))
        hc.ensure_hermes_home()
        for p, m in mtimes.items():
            assert p.stat().st_mtime_ns == m, p
        assert not (home / "topology.yaml").exists()
        assert not (home / "runbooks").exists()


class TestBannerGates:
    def test_fresh_default_profile_gates_on(self, tmp_path, monkeypatch):
        _fresh_home(tmp_path, monkeypatch)
        hc.ensure_hermes_home()
        state = banner._load_banner_state()
        assert state["ops_enabled"] is True
        assert state["matrix_enabled"] is True
        assert state["topology_enabled"] is True
        assert state["env"] == "test"

    def test_ops_explicitly_disabled_gates_off(self, tmp_path, monkeypatch):
        home = _fresh_home(tmp_path, monkeypatch)
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(yaml.safe_dump({
            "ops": {"topology": {"enabled": False}, "permissions": {"enabled": False}},
        }), encoding="utf-8")
        hc.ensure_hermes_home()
        state = banner._load_banner_state()
        assert state["ops_enabled"] is False
        assert state["matrix_enabled"] is False


class TestSoulPersona:
    def test_resolver_ops_section_ops_persona(self):
        assert default_soul_for_config({"ops": {"permissions": {"env": "test"}}}) == DEFAULT_SOUL_MD

    def test_resolver_no_ops_section_generic_persona(self):
        assert default_soul_for_config({}) == DEFAULT_SOUL_MD_GENERIC
        assert default_soul_for_config({"ops": {}}) == DEFAULT_SOUL_MD_GENERIC
        assert default_soul_for_config({"tools": {}}) == DEFAULT_SOUL_MD_GENERIC

    def test_fresh_install_seeds_ops_persona(self, tmp_path, monkeypatch):
        """首装无 config.yaml → DEFAULT_CONFIG 含 ops 段 → ops persona。"""
        home = _fresh_home(tmp_path, monkeypatch)
        hc.ensure_hermes_home()
        assert (home / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD

    def test_legacy_soul_upgraded_to_generic_when_no_ops(self, tmp_path, monkeypatch):
        """config.yaml 明确无 ops 段 + 旧注释模板 SOUL → 升级为通用 persona。"""
        home = _fresh_home(tmp_path, monkeypatch)
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
        (home / "SOUL.md").write_text(_LEGACY_TEMPLATE_SOULS[0], encoding="utf-8")
        hc.ensure_hermes_home()
        assert (home / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD_GENERIC

    def test_legacy_soul_upgraded_to_ops_when_ops_present(self, tmp_path, monkeypatch):
        home = _fresh_home(tmp_path, monkeypatch)
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            "ops:\n  permissions:\n    env: test\n", encoding="utf-8"
        )
        (home / "SOUL.md").write_text(_LEGACY_TEMPLATE_SOULS[0], encoding="utf-8")
        hc.ensure_hermes_home()
        assert (home / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD

    def test_custom_soul_never_overwritten(self, tmp_path, monkeypatch):
        home = _fresh_home(tmp_path, monkeypatch)
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("model: x\n", encoding="utf-8")
        (home / "SOUL.md").write_text("my custom persona\n", encoding="utf-8")
        hc.ensure_hermes_home()
        assert (home / "SOUL.md").read_text(encoding="utf-8").strip() == "my custom persona"
