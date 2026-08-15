"""UI 壳第四批：新前端完全自绘（无 Vigil 主题系统），``dashboard.theme`` 保持
API 兼容——后端 ``_BUILTIN_DASHBOARD_THEMES`` 与 ``GET /api/dashboard/themes``
仍返回正常结构（新前端不使用主题机，仅保证不破坏既有 API）。

覆盖：``_BUILTIN_DASHBOARD_THEMES`` 含 vigil-console / vigil-console-dark 且
旧默认改名（Vigil Blue-Grey Legacy）；``dashboard.theme`` 配置缺省 vigil-console；
``GET /api/dashboard/themes`` 返回新主题并默认激活 vigil-console。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _backend_theme_names() -> set[str]:
    from hermes_cli.web_server import _BUILTIN_DASHBOARD_THEMES
    return {t["name"] for t in _BUILTIN_DASHBOARD_THEMES}


class TestBackendThemeList:
    def test_vigil_console_is_default_and_listed(self):
        names = _backend_theme_names()
        assert "vigil-console" in names
        assert "vigil-console-dark" in names

    def test_legacy_default_kept_with_legacy_label(self):
        from hermes_cli.web_server import _BUILTIN_DASHBOARD_THEMES
        default_entry = next(t for t in _BUILTIN_DASHBOARD_THEMES if t["name"] == "default")
        assert "Legacy" in default_entry["label"]

    def test_theme_list_is_api_compatible(self):
        """第四批新前端无主题注册表；后端名单仅作 API 兼容保留（结构正常）。"""
        names = _backend_theme_names()
        assert names  # 非空
        assert "vigil-console" in names
        assert "default" in names


class TestConfigDefault:
    def test_dashboard_theme_config_default_is_vigil_console(self):
        src = (REPO_ROOT / "hermes_cli" / "web_server.py").read_text(encoding="utf-8")
        # 两处（get_dashboard_themes + bootstrap）都默认 vigil-console。
        assert src.count('cfg_get(config, "dashboard", "theme", default="vigil-console")') >= 2
        assert 'default="default"' not in re.findall(
            r'cfg_get\(config, "dashboard", "theme", default="[^"]*"\)', src)

    def test_config_schema_options_include_new_themes(self):
        src = (REPO_ROOT / "hermes_cli" / "web_server.py").read_text(encoding="utf-8")
        assert '"vigil-console"' in src
        assert '"vigil-console-dark"' in src


class TestThemesEndpoint:
    def test_themes_endpoint_lists_and_activates_vigil_console(self, monkeypatch):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_cli.web_server as ws
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        # 无配置 → active 回落 vigil-console。
        monkeypatch.setattr(
            ws, "load_config", lambda: {},
            raising=False,
        )
        client = TestClient(app)
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        resp = client.get("/api/dashboard/themes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active"] == "vigil-console"
        names = {t["name"] for t in body["themes"]}
        assert {"vigil-console", "vigil-console-dark"} <= names
