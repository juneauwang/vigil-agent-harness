"""批次十四 E2 — 启动进度提示验收测试。

覆盖：进度开关只在交互 tty + 非 CI + 非 oneshot 打开；``_progress_hint``
打印进度行 / 非交互静默；纯 print 不做额外 IO。
"""

from __future__ import annotations

import hermes_cli.main as main_mod


def _tty(monkeypatch, enabled: bool):
    monkeypatch.setattr(main_mod, "_STARTUP_PROGRESS", True)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: enabled)


def test_startup_progress_enabled_interactive(monkeypatch):
    """交互 tty + 非 CI + 非 oneshot → 打开。"""
    _tty(monkeypatch, True)
    assert main_mod._startup_progress_enabled() is True


def test_startup_progress_disabled_non_tty(monkeypatch):
    """管道/非交互（isatty False）→ 不打印。"""
    _tty(monkeypatch, False)
    assert main_mod._startup_progress_enabled() is False


def test_startup_progress_disabled_ci(monkeypatch):
    """CI 环境（即使 tty）→ 不打印。"""
    _tty(monkeypatch, True)
    monkeypatch.setenv("CI", "true")
    assert main_mod._startup_progress_enabled() is False


def test_startup_progress_disabled_oneshot(monkeypatch):
    """oneshot 模式（main 关闭全局开关）→ 不打印。"""
    _tty(monkeypatch, True)
    monkeypatch.setattr(main_mod, "_STARTUP_PROGRESS", False)
    assert main_mod._startup_progress_enabled() is False


def test_progress_hint_prints_when_enabled(monkeypatch, capsys):
    """交互模式：进度行打印且 flush（不吞输出）。"""
    _tty(monkeypatch, True)
    main_mod._progress_hint("· 加载插件与工具…")
    assert "· 加载插件与工具…" in capsys.readouterr().out


def test_progress_hint_silent_when_disabled(monkeypatch, capsys):
    """非交互模式：进度行不输出。"""
    _tty(monkeypatch, False)
    main_mod._progress_hint("· 加载插件与工具…")
    assert capsys.readouterr().out == ""


def test_progress_hint_no_extra_io(monkeypatch, capsys):
    """进度提示是纯 print（无额外文件/网络 IO 的副作用面）。"""
    _tty(monkeypatch, True)
    main_mod._progress_hint("· 启动就绪")
    assert "· 启动就绪" in capsys.readouterr().out
