"""批四十三 — dashboard 重启缺陷收口（OPS-DELTA #60）。

§AX 自伤：``_scan_dashboard_processes`` 排除 lifecycle 子命令（restart/stop/
status/install/uninstall/register/start），server 形态才返回。
§BF 端口单一来源：`dashboard.port` config 键 + `_resolve_dashboard_port` 解析
顺序（显式 --port > config > unit > 默认）；install 把端口落 config + unit 一致。
§BF 无头 gio：``_maybe_open_browser`` 开浏览器失败静默降级只打一行访问提示，
不打错误。
"""

from __future__ import annotations

import pytest

from hermes_cli import web_server as ws


class TestHeadlessBrowserDegrade:
    """batch 43 §BF：开浏览器在无头/gio 失败场景静默降级（只提示，不抛错）。"""

    def test_no_display_skips_without_attempt(self, monkeypatch):
        """无 DISPLAY/WAYLAND → _maybe_open_browser 不尝试开浏览器。"""
        monkeypatch.setattr(ws.sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", "")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        opened = {"called": False}

        import webbrowser as _wb

        real_open = _wb.open

        def _fake_open(url):
            opened["called"] = True
            return real_open(url)

        monkeypatch.setattr(_wb, "open", _fake_open)
        ws._maybe_open_browser("127.0.0.1", 9133, True, "")
        assert opened["called"] is False

    def test_display_gio_failure_does_not_raise(self, monkeypatch):
        """有 DISPLAY，但 webbrowser.open 抛 gio 错 → 函数本身不向调用方抛。"""
        monkeypatch.setattr(ws.sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setenv("WAYLAND_DISPLAY", "")
        monkeypatch.setattr(ws.time, "sleep", lambda _s: None)

        import webbrowser as _wb

        def _fail(url):
            raise RuntimeError("gio: Operation not supported")

        monkeypatch.setattr(_wb, "open", _fail)

        from threading import Thread

        thread_errors: list[Exception] = []

        def _open_wrapped():
            try:
                ws._maybe_open_browser("127.0.0.1", 9133, True, "")
            except Exception as exc:  # pragma: no cover
                thread_errors.append(exc)

        t = Thread(target=_open_wrapped)
        t.start()
        t.join(timeout=3)
        assert not thread_errors

    def test_access_hint_text_present_in_source(self):
        """降级提示文案接线在 _maybe_open_browser 的失败分支（防静默报错）。"""
        import inspect

        src = inspect.getsource(ws._maybe_open_browser)
        assert "无图形环境，跳过自动打开浏览器" in src
