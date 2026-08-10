"""Unified Vigil console header — one visual language for every profile."""

import io
from unittest.mock import patch

import yaml
from rich.console import Console

import hermes_cli.banner as banner


def _fresh_state():
    """Clear the banner-state TTL cache between tests."""
    banner._banner_state_cache = None


def _render(state=None, *, model="gpt-5", cwd="/srv/ops", session_id="ops-001"):
    if state is None:
        state = _ops_state(
            ops_enabled=False, env="", matrix_enabled=False,
            topology_enabled=False, runbook_enabled=False,
            entity_count=0, runbook_count=0,
            profile="default", home="/home/ops/.hermes",
        )
    with (
        patch.object(banner, "_load_banner_state", return_value=state),
        patch.object(banner, "format_banner_version_label", return_value="Vigil v0.1.0 (test)"),
        patch.object(banner, "get_git_banner_state", return_value=None),
        patch.object(banner, "get_latest_release_tag", return_value=None),
        patch.object(banner, "get_available_skills", return_value={"general": ["skill-a"]}),
    ):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=True, color_system="truecolor", width=120)
        banner.build_welcome_banner(
            console=console, model=model, cwd=cwd,
            session_id=session_id,
            tools=[{"function": {"name": "topo_query"}}],
            get_toolset_for_tool=lambda n: "topo",
        )
        return buf.getvalue()


def _ops_state(**overrides):
    state = {
        "ops_enabled": True,
        "env": "test",
        "matrix_enabled": True,
        "topology_enabled": True,
        "runbook_enabled": True,
        "entity_count": 3,
        "runbook_count": 2,
        "profile": "ops",
        "home": "/home/ops/.hermes/profiles/ops",
    }
    state.update(overrides)
    return state


def test_load_banner_state_reads_profile_snapshot(tmp_path, monkeypatch):
    _fresh_state()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "ops": {
            "topology": {"enabled": True},
            "runbooks": {"enabled": True},
            "permissions": {"enabled": True, "env": "uat", "role": "uat"},
        },
    }), encoding="utf-8")
    (tmp_path / "topology.yaml").write_text(yaml.safe_dump({
        "core_entities": [
            {"name": "a", "type": "svc"},
            {"name": "b", "type": "db"},
        ],
    }), encoding="utf-8")
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "runbooks" / "y.yaml").write_text("b: 2\n", encoding="utf-8")

    state = banner._load_banner_state()

    assert state["ops_enabled"] is True
    assert state["env"] == "uat"
    assert state["matrix_enabled"] is True
    assert state["entity_count"] == 2
    assert state["runbook_count"] == 2
    assert state["home"] == str(tmp_path)


def test_load_banner_state_returns_off_dict_when_ops_off(tmp_path, monkeypatch):
    _fresh_state()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "ops": {"topology": {"enabled": False}, "permissions": {"enabled": False}},
    }), encoding="utf-8")

    state = banner._load_banner_state()

    assert state is not None
    assert state["ops_enabled"] is False
    assert state["env"] == ""
    assert state["matrix_enabled"] is False


def test_unified_banner_renders_console_header_for_ops_profile():
    out = _render(_ops_state())

    assert "PROFILE" in out
    assert "ENV" in out
    assert "[test]" in out
    assert "matrix ON" in out
    assert "3 entities" in out
    assert "2 loaded" in out
    assert "topo_query" in out
    # The assistant dashboard is gone for every profile now
    assert "Available Tools" not in out
    assert "Available Skills" not in out


def test_unified_banner_renders_custom_env_badge():
    """自定义环境名（bare_metal_prod）渲染进 ENV badge，不锁死 test/uat/prod。"""
    out = _render(_ops_state(env="bare_metal_prod"))

    assert "ENV" in out
    assert "[bare_metal_prod]" in out
    assert "matrix ON" in out


def test_unified_banner_matrix_off_warns():
    out = _render(_ops_state(matrix_enabled=False))

    assert "matrix OFF" in out
    assert "权限矩阵未启用" in out


def test_unified_banner_non_ops_shares_console_header():
    _fresh_state()
    out = _render(None)

    # Same console header shape — no Hermes-style assistant dashboard
    assert "Available Tools" not in out
    assert "Available Skills" not in out
    assert "GATES" in out
    assert "off（未启用 ops harness）" in out
    # Capability summary + onboarding hint replace the tool/skill inventory
    assert "1 tools · 1 skills" in out
    assert "vigil ops-init" in out


def test_load_banner_state_matrix_default_on_without_enabled_key(tmp_path, monkeypatch):
    """OPS-DELTA #1：矩阵默认启用——config 无 enabled 键也显示 matrix ON。"""
    _fresh_state()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "ops": {
            "permissions": {"env": "prod", "role": "prod"},
        },
    }), encoding="utf-8")

    state = banner._load_banner_state()

    assert state["ops_enabled"] is False  # 无显式 ops 开关 → 非 ops banner 布局
    assert state["matrix_enabled"] is True  # 但矩阵语义为默认启用


def test_unified_banner_no_topology_guides_ops_init():
    """数据缺失时 TOPOLOGY 段引导运行 vigil ops-init（不再要求先启用能力）。"""
    out = _render(_ops_state(entity_count=0, runbook_count=0))

    assert "no topology — run vigil ops-init" in out
