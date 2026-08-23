"""批次四十（§AS C3）：platform_toolsets 并集迁移 + kanban 隐式展开核查。

覆盖：
  - 存量显式 ``platform_toolsets.cli: [hermes-cli]``（v33）→ 迁移后含
    topo/runbook 默认项，results.config_added 带 diff 提示；
  - 已含默认项 → 不动、无 diff；
  - 无 platform_toolsets → 不产生 defaults dump；
  - 迁移后显式列表 == 默认值时被 default-stripping 移除（读取仍生效）；
  - kanban 隐式展开核查：_get_platform_tools 含 kanban 是复合 toolset 展开
    （意图内——schema 由 check_fn 门控），无 VIGIL_KANBAN_TASK/无 profile
    显式启用时 kanban 工具不出现在正常 CLI（无意外冒出）。
"""

from __future__ import annotations

import os

import pytest
import yaml

import hermes_cli.config as hc
from hermes_cli.config_migrations import run_migrations


@pytest.fixture(autouse=True)
def _home_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _write(tmp_path, data: dict) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    hc._LOAD_CONFIG_CACHE.clear()


def _raw(tmp_path) -> dict:
    return yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8")) or {}


class TestUnionMigration:
    def test_explicit_list_gets_defaults_merged(self, tmp_path):
        _write(tmp_path, {
            "_config_version": 33,
            "platform_toolsets": {"cli": ["hermes-cli"]},
            "model": {"default": "gpt-4o"},
        })
        results = hc.migrate_config(interactive=False, quiet=True)
        raw = _raw(tmp_path)
        assert raw["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook", "matrix"]
        assert raw["_config_version"] == 34
        assert any("platform_toolsets.cli" in c and "topo" in c for c in results["config_added"])

    def test_already_contains_defaults_no_diff(self, tmp_path):
        _write(tmp_path, {
            "_config_version": 33,
            "platform_toolsets": {"cli": ["hermes-cli", "topo", "runbook", "matrix"]},
        })
        results = hc.migrate_config(interactive=False, quiet=True)
        assert not any("platform_toolsets.cli" in c for c in results["config_added"])

    def test_mixed_explicit_list_merges_missing_only(self, tmp_path):
        _write(tmp_path, {
            "_config_version": 33,
            "platform_toolsets": {"cli": ["hermes-cli", "clarify"]},
        })
        hc.migrate_config(interactive=False, quiet=True)
        raw = _raw(tmp_path)
        cli = raw["platform_toolsets"]["cli"]
        assert set(cli) == {"hermes-cli", "clarify", "topo", "runbook", "matrix"}

    def test_no_platform_toolsets_untouched(self, tmp_path):
        _write(tmp_path, {"_config_version": 33, "model": {"default": "gpt-4o"}})
        hc.migrate_config(interactive=False, quiet=True)
        raw = _raw(tmp_path)
        assert "platform_toolsets" not in raw  # 不产生 defaults dump

    def test_union_equal_to_default_no_defaults_dump(self, tmp_path):
        """并集结果 == 默认 → 显式键保留（用户显式写过），但不产生 defaults
        dump——其余 schema 默认键不落盘，读取时 deep-merge 生效。"""
        _write(tmp_path, {
            "_config_version": 33,
            "platform_toolsets": {"cli": ["hermes-cli"]},
        })
        hc.migrate_config(interactive=False, quiet=True)
        raw = _raw(tmp_path)
        assert raw["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook", "matrix"]
        # 不产生 defaults dump：schema 默认顶键不落盘
        for default_key in ("tts", "compression", "security", "whatsapp", "bedrock"):
            assert default_key not in raw
        from hermes_cli.tools_config import _get_platform_tools
        enabled = _get_platform_tools(hc.load_config(), "cli")
        assert {"topo", "runbook"} <= enabled


class TestKanbanExpansionIntent:
    def test_kanban_toolset_expands_but_schema_stays_gated(self, tmp_path, monkeypatch):
        """隐式展开符合意图：工具集名在 enabled 集（零 footprint），但无
        VIGIL_KANBAN_TASK / 无 profile 显式启用时 check_fn 关闭 → 无工具冒出。"""
        monkeypatch.delenv("VIGIL_KANBAN_TASK", raising=False)
        from hermes_cli.tools_config import _get_platform_tools
        from tools import kanban_tools

        enabled = _get_platform_tools(hc.load_config(), "cli")
        # 复合 toolset 展开（kanban_* 在 _VIGIL_CORE_TOOLS）——意图内
        assert "kanban" in enabled
        # 但 schema 门控关闭：普通 CLI 会话零 kanban 工具
        assert kanban_tools._check_kanban_mode() is False
        assert kanban_tools._check_kanban_orchestrator_mode() is False

    def test_run_migrations_direct_drive(self, tmp_path):
        """直接驱动 registry（测试套件惯例）：v33 → 并集生效。"""
        _write(tmp_path, {
            "_config_version": 33,
            "platform_toolsets": {"cli": ["hermes-cli"]},
        })
        results = {"env_added": [], "config_added": [], "warnings": []}
        run_migrations(33, results, quiet=True)
        raw = _raw(tmp_path)
        assert raw["platform_toolsets"]["cli"] == ["hermes-cli", "topo", "runbook", "matrix"]
        assert any("topo" in c for c in results["config_added"])
