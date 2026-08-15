"""批次二十九：setup 工具面净化 + Nous 残留移除 + cua-driver 禁用。

OPS-DELTA #48。断言：
1. 默认勾选 = 运维工具集（含 prom），bfl/tts 与消费级工具默认关闭；
2. 工具选择清单 = 运维区 + "高级 / 娱乐工具" 折叠区（可手动勾选，不默认）；
3. 各 provider 选择界面无 "Nous Subscription / Nous Portal" 死入口；
   Search provider 默认高亮 = 免费无 key 的 ddgs，运行时兜底也是 ddgs；
4. setup 全链路无 "Nous Subscription / billed to your subscription" 字样；
5. cua-driver：默认工具集/首次安装路径不触发安装（computer_use 默认关，
   仅用户显式启用时走 post_setup 安装）。
"""

from unittest.mock import patch

import pytest

from hermes_cli.nous_account import NousPortalAccountInfo
from hermes_cli.nous_subscription import NousSubscriptionFeatures
from hermes_cli.tools_config import (
    _ADVANCED_TOOLSETS,
    _DEFAULT_OFF_TOOLSETS,
    _OPS_CORE_TOOLSETS,
    _RECENTLY_SHIPPED_TOOLSETS,
    _get_platform_tools,
    _prompt_toolset_checklist,
    _visible_providers,
    CONFIGURABLE_TOOLSETS,
    TOOL_CATEGORIES,
)

_LOGGED_OUT_FEATURES = NousSubscriptionFeatures(
    subscribed=False,
    nous_auth_present=False,
    provider_is_nous=False,
    features={},
    account_info=NousPortalAccountInfo(
        logged_in=False,
        source="none",
        fresh=False,
        paid_service_access=None,
    ),
)

_OPTI = {
    "terminal", "file", "code_execution", "web", "skills", "todo", "memory",
    "session_search", "cronjob", "delegation", "vision", "prom",
}

# context_engine is engine-gated (auto-enabled only when a non-default
# context engine is active), so it is folded in the checklist but not a
# member of _DEFAULT_OFF_TOOLSETS.
_CONSUMER = {
    "video", "image_gen", "video_gen", "bfl", "x_search", "tts",
    "homeassistant", "spotify", "discord", "discord_admin",
    "yuanbao", "computer_use", "browser",
}


def _fresh_cli_enabled() -> set:
    return _get_platform_tools(
        {"platform_toolsets": {"cli": ["hermes-cli"]}},
        "cli",
        include_default_mcp_servers=False,
    )


class TestDefaultToolsetPreselect:
    def test_fresh_install_preselects_ops_core(self):
        enabled = _fresh_cli_enabled()
        # prom is checklist-pre-selected for first-time users but not part of
        # the hermes-cli composite, so the raw runtime set is ops-core minus
        # prom (see TestFirstInstallPreselection for the checklist side).
        assert _OPTI - {"prom"} <= enabled

    def test_fresh_install_excludes_bfl_and_tts(self):
        enabled = _fresh_cli_enabled()
        assert "bfl" not in enabled
        assert "tts" not in enabled

    def test_fresh_install_excludes_consumer_tools(self):
        enabled = _fresh_cli_enabled()
        assert not (_CONSUMER & enabled)

    def test_default_off_set_covers_bfl_tts_and_consumer(self):
        assert {"bfl", "tts"} <= _DEFAULT_OFF_TOOLSETS
        assert _CONSUMER <= _DEFAULT_OFF_TOOLSETS

    def test_recently_shipped_set_is_empty(self):
        # bfl 早已过 back-fill 窗口（v0.1.0 基线前发布），且本批改为默认关——
        # 保留该集合会把默认关又拉回开。
        assert _RECENTLY_SHIPPED_TOOLSETS == frozenset()


class TestChecklistSurface:
    def _capture_checklist(self, enabled, platform="cli"):
        captured = {}

        def fake_checklist(title, labels, pre_selected, **kwargs):
            captured["title"] = title
            captured["labels"] = list(labels)
            captured["pre_selected"] = set(pre_selected)
            # Confirm the pre-selected defaults (accept-as-is).
            return set(pre_selected)

        with patch(
            "hermes_cli.curses_ui.curses_checklist",
            side_effect=fake_checklist,
        ), patch(
            "hermes_cli.tools_config._estimate_tool_tokens",
            return_value={},
        ):
            result = _prompt_toolset_checklist(
                "cli", enabled, "cli", force_fresh=False
            )
        captured["result"] = result
        return captured

    def test_title_is_vigil_ops_context(self):
        captured = self._capture_checklist(set())
        assert "Vigil" in captured["title"]

    def test_ops_core_listed_first_with_advanced_separator(self):
        captured = self._capture_checklist(set())
        labels = captured["labels"]
        sep = next(
            i for i, label in enumerate(labels) if label.startswith("── 高级")
        )
        label_by_key = {k: l for k, l, _ in CONFIGURABLE_TOOLSETS}
        # ops core rows come before the separator, advanced rows after it.
        assert any(l.startswith(label_by_key["terminal"]) for l in labels[:sep])
        assert any(l.startswith(label_by_key["prom"]) for l in labels[:sep])
        assert any(l.startswith(label_by_key["bfl"]) for l in labels[sep + 1 :])
        assert any(l.startswith(label_by_key["browser"]) for l in labels[sep + 1 :])
        # separator itself is a label row (not a toolset row)
        assert "高级 / 娱乐工具" in labels[sep]

    def test_separator_row_is_not_a_toolset_and_ignored(self):
        captured = self._capture_checklist(set(_OPS_CORE_TOOLSETS))
        # Pre-selected defaults = ops core only (no advanced tools).
        selected = captured["pre_selected"]
        labels = captured["labels"]
        sep_idx = next(i for i, l in enumerate(labels) if l.startswith("── 高级"))
        assert sep_idx not in selected
        assert captured["result"] == set(_OPS_CORE_TOOLSETS)

    def test_advanced_tools_still_manually_enableable(self):
        captured = self._capture_checklist(set(_OPS_CORE_TOOLSETS) | {"bfl"})
        # A manually-enabled advanced tool stays in the pre-selected set.
        assert captured["result"] == set(_OPS_CORE_TOOLSETS) | {"bfl"}
def _provider_names(cat_key):
    return [
        p["name"]
        for p in _visible_providers(
            TOOL_CATEGORIES[cat_key], {}, features=_LOGGED_OUT_FEATURES
        )
    ]


class TestNoNousDeadEntries:
    def test_no_managed_nous_rows_remain_in_categories(self):
        for cat in TOOL_CATEGORIES.values():
            for provider in cat.get("providers", []):
                assert not provider.get("managed_nous_feature"), cat
                assert not provider.get("requires_nous_auth"), cat
                assert "Nous" not in provider.get("name", ""), cat

    @pytest.mark.parametrize("cat_key", ["tts", "stt", "web", "browser"])
    def test_picker_has_no_nous_option(self, cat_key):
        names = _provider_names(cat_key)
        assert not any("Nous" in n for n in names), names

    def test_web_picker_default_highlight_is_free_no_key_ddgs(self):
        providers = _visible_providers(
            TOOL_CATEGORIES["web"], {}, features=_LOGGED_OUT_FEATURES
        )
        assert providers[0].get("web_backend") == "ddgs"
        assert "Firecrawl Self-Hosted" in [p["name"] for p in providers]

    def test_tts_picker_starts_with_free_edge(self):
        providers = _visible_providers(
            TOOL_CATEGORIES["tts"], {}, features=_LOGGED_OUT_FEATURES
        )
        assert providers[0].get("tts_provider") == "edge"

    def test_browser_picker_starts_with_local_browser(self):
        providers = _visible_providers(
            TOOL_CATEGORIES["browser"], {}, features=_LOGGED_OUT_FEATURES
        )
        assert providers[0].get("browser_provider") == "local"

    def test_image_gen_video_gen_have_no_hardcoded_providers(self):
        assert TOOL_CATEGORIES["image_gen"]["providers"] == []
        assert TOOL_CATEGORIES["video_gen"]["providers"] == []

    def test_setup_visible_text_has_no_subscription_phrasing(self):
        # Provider picker rows (name/badge/tag) must not advertise a Nous
        # subscription in the default setup flow. Scope: the four categories
        # the batch named (TTS/STT/Web/Browser). image_gen/video_gen plugin
        # rows keep their managed-gateway capability (plugins out of scope —
        # see OPS-DELTA #48 boundary note) and are only reachable via the
        # folded advanced section + explicit opt-in.
        for cat_key in ("tts", "stt", "web", "browser"):
            for p in _visible_providers(
                TOOL_CATEGORIES[cat_key], {}, features=_LOGGED_OUT_FEATURES
            ):
                blob = " ".join(
                    str(p.get(field, "")) for field in ("name", "badge", "tag")
                )
                assert "Nous Subscription" not in blob, (cat_key, blob)
                assert "Nous Portal" not in blob, (cat_key, blob)
                assert "billed to your subscription" not in blob, (cat_key, blob)


class TestFirstInstallPreselection:
    def test_first_install_preselects_ops_core_including_prom(self):
        """首次安装清单预选 = 运维工具集（含 prom），bfl/tts 不勾选。"""
        import hermes_cli.tools_config as tc

        config = {"platform_toolsets": {"cli": ["hermes-cli"]}}
        captured = {}

        def fake_checklist(platform_label, enabled, platform="cli", **kwargs):
            # tools_command passes the pre-selection as the ``enabled`` set.
            captured["pre_selected"] = set(enabled)
            return set(enabled)

        with patch(
            "hermes_cli.tools_config._prompt_toolset_checklist",
            side_effect=fake_checklist,
        ), patch.object(
            tc, "apply_nous_managed_defaults", return_value=set()
        ), patch.object(
            tc, "_configure_toolset", side_effect=lambda ts_key, config: None
        ), patch.object(
            tc, "save_config", side_effect=lambda cfg: None
        ), patch.object(
            tc, "_get_enabled_platforms", return_value=["cli"]
        ):
            tc.tools_command(first_install=True, config=config)

        assert _OPTI <= captured["pre_selected"]
        assert not ({"bfl", "tts"} & captured["pre_selected"])
        assert not (_CONSUMER & captured["pre_selected"])


class TestWebRuntimeDefault:
    def test_get_backend_falls_back_to_free_ddgs(self):
        import tools.web_tools as wt

        with patch.object(wt, "_load_web_config", return_value={}), patch.object(
            wt, "_has_env", return_value=False
        ), patch.object(wt, "_is_tool_gateway_ready", return_value=False), patch.object(
            wt, "_ddgs_package_importable", return_value=False
        ), patch.object(
            wt, "_list_registered_web_providers", return_value=[]
        ):
            assert wt._get_backend() == "ddgs"


class TestCuaDriverNoAutoInstall:
    def test_computer_use_is_default_off(self):
        assert "computer_use" in _DEFAULT_OFF_TOOLSETS
        assert "computer_use" not in _fresh_cli_enabled()

    def test_first_install_never_triggers_cua_driver_install(self):
        """首次安装默认流程（运维集）不得触发 cua-driver 下载/安装。

        install_cua_driver 只应在用户显式启用 computer_use 工具集的
        post_setup("cua_driver") 路径被调用。
        """
        import hermes_cli.tools_config as tc

        config = {"platform_toolsets": {"cli": ["hermes-cli"]}}

        with patch(
            "hermes_cli.tools_config._prompt_toolset_checklist",
            return_value=set(_OPS_CORE_TOOLSETS),
        ), patch(
            "hermes_cli.tools_config.apply_nous_managed_defaults",
            return_value=set(),
        ), patch(
            "hermes_cli.tools_config._configure_toolset",
            side_effect=lambda ts_key, config: None,
        ), patch(
            "hermes_cli.tools_config.install_cua_driver",
            side_effect=AssertionError("cua-driver must not auto-install"),
        ), patch(
            "hermes_cli.tools_config.save_config",
            side_effect=lambda cfg: None,
        ), patch.object(tc, "_get_enabled_platforms", return_value=["cli"]):
            tc.tools_command(first_install=True, config=config)

        assert "computer_use" not in config["platform_toolsets"]["cli"]
