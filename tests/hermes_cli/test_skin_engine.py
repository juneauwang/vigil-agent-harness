"""Tests for hermes_cli.skin_engine — the data-driven skin/theme system."""

import pytest


@pytest.fixture(autouse=True)
def reset_skin_state(tmp_path, monkeypatch):
    """Reset skin engine state and isolate Vigil skin storage between tests."""
    from hermes_cli import skin_engine
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    skin_engine._active_skin = None
    skin_engine._active_skin_name = "default"
    yield
    skin_engine._active_skin = None
    skin_engine._active_skin_name = "default"


class TestSkinConfig:
    def test_default_skin_has_required_fields(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("default")
        assert skin.name == "default"
        assert skin.tool_prefix == "┊"
        assert "banner_title" in skin.colors
        assert "banner_border" in skin.colors
        assert "agent_name" in skin.branding


    def test_get_spinner_wings_empty_for_default(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("default")
        assert skin.get_spinner_wings() == []


class TestBuiltinSkins:
    def test_vigil_skin_loads(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("vigil")
        assert skin.name == "vigil"
        assert skin.tool_prefix == "┊"
        # Blue-black identity: border stays blue-dominant (exact values are
        # owned by the palette audit in test_skin_palettes.py, which enforces
        # contrast floors — don't pin literals here).
        border = skin.get_color("banner_border")
        r, g, b = (int(border[i:i + 2], 16) for i in (1, 3, 5))
        assert b > r, f"vigil border lost its blue: {border}"
        assert skin.get_color("response_border") == "#4A90D9"
        assert skin.get_color("session_label") == "#8FB8E8"
        assert skin.get_branding("prompt_symbol") == "◉ Vigil >"

    def test_unknown_skin_falls_back_to_vigil_palette(self):
        from hermes_cli.skin_engine import load_skin
        # 已删除的内置皮肤（slate/ares/...）→ 回退 vigil 色值，不抛异常。
        for name in ("slate", "ares", "mono", "daylight", "charizard"):
            skin = load_skin(name)
            assert skin.get_color("banner_title") == "#8FB8E8"  # vigil 蓝

    def test_default_is_vigil_blue_black(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("default")
        assert skin.name == "default"
        assert skin.get_color("banner_title") == "#8FB8E8"
        assert skin.get_color("ui_accent") == "#4A90D9"








class TestSkinManagement:
    def test_set_active_skin(self):
        from hermes_cli.skin_engine import set_active_skin, get_active_skin, get_active_skin_name
        skin = set_active_skin("vigil")
        assert skin.name == "vigil"
        assert get_active_skin_name() == "vigil"
        assert get_active_skin().name == "vigil"


    def test_list_skins_includes_builtins(self):
        from hermes_cli.skin_engine import list_skins
        skins = list_skins()
        names = [s["name"] for s in skins]
        assert names == ["default", "vigil"]
        assert not ({"ares", "mono", "slate", "daylight", "warm-lightmode",
                     "poseidon", "sisyphus", "charizard"} & set(names))
        for s in skins:
            assert "source" in s
            assert s["source"] == "builtin"

    def test_config_skin_deleted_name_startup_ok_and_warns_once(self, tmp_path, monkeypatch):
        """display.skin 引用已删除皮肤（slate）→ init 不崩、回退 vigil、警告一次。"""
        from hermes_cli import skin_engine
        skin_engine._UNKNOWN_SKIN_WARNED.clear()
        monkeypatch.setattr(
            skin_engine, "_skins_dir", lambda: tmp_path / "empty-skins"
        )
        warns = []
        monkeypatch.setattr(
            skin_engine.logger,
            "warning",
            lambda msg, *args: warns.append(msg % args if args else msg),
        )
        try:
            skin_engine.init_skin_from_config({"display": {"skin": "slate"}})
            skin_engine.init_skin_from_config({"display": {"skin": "slate"}})
        finally:
            skin_engine._UNKNOWN_SKIN_WARNED.clear()

        assert skin_engine.get_active_skin_name() == "slate"
        assert skin_engine.get_active_skin().get_color("banner_title") == "#8FB8E8"
        assert len([w for w in warns if "not found" in w]) == 1




class TestUserSkins:
    def test_load_user_skin_from_yaml(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin
        # Create a user skin YAML
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        skin_file = skins_dir / "custom.yaml"
        skin_data = {
            "name": "custom",
            "description": "A custom test skin",
            "colors": {"banner_title": "#FF0000"},
            "branding": {"agent_name": "Custom Agent"},
            "tool_prefix": "▸",
        }
        import yaml
        skin_file.write_text(yaml.dump(skin_data))

        # Patch skins dir
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)

        skin = load_skin("custom")
        assert skin.name == "custom"
        assert skin.get_color("banner_title") == "#FF0000"
        assert skin.get_branding("agent_name") == "Custom Agent"
        assert skin.tool_prefix == "▸"
        # Should inherit defaults for unspecified colors
        assert skin.get_color("banner_border") == "#3E6B9B"  # from default (vigil 蓝黑)

    def test_load_user_skin_invalid_section_types_fall_back_to_defaults(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin

        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        import yaml

        (skins_dir / "broken.yaml").write_text(
            yaml.dump(
                {
                    "name": "broken",
                    "colors": ["not", "a", "mapping"],
                    "spinner": "invalid",
                    "branding": ["also", "invalid"],
                    "tool_emojis": ["invalid"],
                    "tool_prefix": "!",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)

        skin = load_skin("broken")

        assert skin.name == "broken"
        assert skin.get_color("banner_title") == "#8FB8E8"
        assert skin.get_branding("agent_name") == "Vigil"
        # 无效 spinner 段被忽略 → 继承 default（vigil）的 spinner。
        assert skin.spinner.get("waiting_faces") == ["(·)", "(·|)", "(·/)", "(·\\)"]
        assert skin.tool_emojis == {}
        assert skin.tool_prefix == "!"

    def test_list_skins_includes_user_skins(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import list_skins
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        import yaml
        (skins_dir / "pirate.yaml").write_text(yaml.dump({
            "name": "pirate",
            "description": "Arr matey",
        }))
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)

        skins = list_skins()
        names = [s["name"] for s in skins]
        assert "pirate" in names
        pirate = [s for s in skins if s["name"] == "pirate"][0]
        assert pirate["source"] == "user"


class TestDisplayIntegration:


    def test_tool_message_uses_skin_prefix(self):
        from hermes_cli.skin_engine import set_active_skin
        from agent.display import get_cute_tool_message
        set_active_skin("vigil")
        msg = get_cute_tool_message("terminal", {"command": "ls"}, 0.5)
        assert msg.startswith("┊")


class TestCliBrandingHelpers:


    def test_active_goodbye_default(self):
        from hermes_cli.skin_engine import set_active_skin, get_active_goodbye

        set_active_skin("default")
        assert get_active_goodbye() == "Goodbye! ⚕"

    def test_prompt_toolkit_style_overrides_cover_tui_classes(self):
        from hermes_cli.skin_engine import set_active_skin, get_prompt_toolkit_style_overrides
        set_active_skin("vigil")
        overrides = get_prompt_toolkit_style_overrides()
        required = {
            "input-area",
            "placeholder",
            "prompt",
            "prompt-working",
            "hint",
            "status-bar",
            "status-bar-strong",
            "status-bar-dim",
            "status-bar-good",
            "status-bar-warn",
            "status-bar-bad",
            "status-bar-critical",
            "input-rule",
            "image-badge",
            "completion-menu",
            "completion-menu.completion",
            "completion-menu.completion.current",
            "completion-menu.meta.completion",
            "completion-menu.meta.completion.current",
            "status-bar",
            "status-bar-strong",
            "status-bar-dim",
            "status-bar-good",
            "status-bar-warn",
            "status-bar-bad",
            "status-bar-critical",
            "voice-status",
            "voice-status-recording",
            "clarify-border",
            "clarify-title",
            "clarify-question",
            "clarify-choice",
            "clarify-selected",
            "clarify-active-other",
            "clarify-countdown",
            "sudo-prompt",
            "sudo-border",
            "sudo-title",
            "sudo-text",
            "approval-border",
            "approval-title",
            "approval-desc",
            "approval-cmd",
            "approval-choice",
            "approval-selected",
        }
        assert required.issubset(overrides.keys())

    def test_prompt_toolkit_style_overrides_use_skin_colors(self):
        from hermes_cli.skin_engine import (
            set_active_skin,
            get_active_skin,
            get_prompt_toolkit_style_overrides,
        )

        set_active_skin("vigil")
        skin = get_active_skin()
        overrides = get_prompt_toolkit_style_overrides()
        assert overrides["prompt"] == skin.get_color("prompt")
        assert overrides["input-rule"] == skin.get_color("input_rule")
        assert overrides["status-bar"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_text')}"
        )
        assert overrides["status-bar-strong"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_strong')} bold"
        )
        assert overrides["status-bar-critical"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_critical')} bold"
        )
        assert overrides["clarify-title"] == f"{skin.get_color('banner_title')} bold"
        assert overrides["sudo-prompt"] == f"{skin.get_color('ui_error')} bold"
        assert overrides["approval-title"] == f"{skin.get_color('ui_warn')} bold"
