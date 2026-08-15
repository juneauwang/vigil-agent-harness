"""方向 1 主题同步验收：Vigil Console 默认主题 + 前后端主题名单不漂移。

覆盖：``_BUILTIN_DASHBOARD_THEMES`` 含 vigil-console / vigil-console-dark 且
旧默认改名（Vigil Blue-Grey Legacy）；前端 presets.ts 的内建主题名与后端名单
一致（解析 presets.ts 源码）；``dashboard.theme`` 配置缺省为 vigil-console；
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


def _frontend_theme_names() -> set[str]:
    """从 presets.ts 源码解析内建主题 name 列表（防止前后端名单漂移）。

    BUILTIN_THEMES 的键既有未加引号的（default: defaultTheme）也有加引号的
    （"vigil-console": vigilConsoleTheme），两种形态都解析。
    """
    src = (REPO_ROOT / "web" / "src" / "themes" / "presets.ts").read_text(encoding="utf-8")
    block = src.split("BUILTIN_THEMES", 1)[1]
    names = re.findall(
        r'^\s{2}(?:"([a-z0-9-]+)"|([a-z0-9-]+)):', block, re.MULTILINE)
    return {a or b for a, b in names}


class TestBackendThemeList:
    def test_vigil_console_is_default_and_listed(self):
        names = _backend_theme_names()
        assert "vigil-console" in names
        assert "vigil-console-dark" in names

    def test_legacy_default_kept_with_legacy_label(self):
        from hermes_cli.web_server import _BUILTIN_DASHBOARD_THEMES
        default_entry = next(t for t in _BUILTIN_DASHBOARD_THEMES if t["name"] == "default")
        assert "Legacy" in default_entry["label"]

    def test_frontend_and_backend_theme_names_in_sync(self):
        backend = _backend_theme_names()
        frontend = _frontend_theme_names()
        # 前端每个内建主题都必须在后端名单里（后端 label/description 由
        # /api/dashboard/themes 下发）；后端名单不应有前端缺失的项。
        assert frontend <= backend, f"前端缺失于后端: {frontend - backend}"
        assert backend <= frontend, f"后端缺失于前端: {backend - frontend}"


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
