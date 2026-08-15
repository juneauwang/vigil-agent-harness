"""``vigil watch`` 子命令测试——unit 文件内容 / uninstall / status / 平台守卫。

不碰真实 systemd：``_systemctl`` / ``_supports_systemd_user`` / ``Path.home``
全部 mock/临时目录。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import hermes_cli.watch as watch_mod


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """临时 HOME（unit 落盘）+ 临时 VIGIL_HOME（watch 数据）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VIGIL_HOME", str(hermes_home))
    return home, hermes_home


@pytest.fixture
def fake_systemd(monkeypatch):
    """默认：支持 systemd，systemctl 全部成功返回。"""
    monkeypatch.setattr(watch_mod, "_supports_systemd_user", lambda: True)

    def _run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="active\n", stderr="")

    monkeypatch.setattr(watch_mod, "_systemctl", _run)
    return _run


class TestWatchInstall:
    def test_unit_content(self, fake_env, fake_systemd):
        home, hermes_home = fake_env
        calls = []
        real_run = fake_systemd

        def _run(args, **kw):
            calls.append(list(args))
            return real_run(args, **kw)

        watch_mod._systemctl = _run
        rc = watch_mod.watch_install()
        assert rc == 0

        unit = Path(home) / ".config" / "systemd" / "user" / "vigil-watch.service"
        assert unit.is_file()
        text = unit.read_text(encoding="utf-8")
        # 服务名固定 vigil-watch（硬约束 7：与 upstream hermes-gateway 无关联）
        assert "vigil-watch.service" in str(unit)
        assert "hermes-gateway" not in str(unit)
        assert f"Environment=VIGIL_HOME={hermes_home}" in text
        # ExecStart 指向 venv python + watch_collect_loop
        assert "-m hermes_cli.watch_collect_loop" in text
        assert text.startswith("[Unit]")
        assert "WantedBy=default.target" in text
        assert "Restart=on-failure" in text
        # enable --now 被调用
        assert any(a[0] == "enable" and a[1] == "--now" for a in calls)

    def test_install_unsupported_platform_errors(self, fake_env, monkeypatch, capsys):
        monkeypatch.setattr(watch_mod, "_supports_systemd_user", lambda: False)
        rc = watch_mod.watch_install()
        assert rc != 0
        out = capsys.readouterr().err
        assert "不支持 systemd" in out
        assert "WSL" in out
        # 不假装成功：unit 不应被写出
        unit = Path(fake_env[0]) / ".config" / "systemd" / "user" / "vigil-watch.service"
        assert not unit.exists()


class TestWatchUninstall:
    def test_uninstall_removes_unit(self, fake_env, fake_systemd):
        home, _ = fake_env
        unit = Path(home) / ".config" / "systemd" / "user" / "vigil-watch.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n", encoding="utf-8")
        rc = watch_mod.watch_uninstall()
        assert rc == 0
        assert not unit.exists()

    def test_uninstall_not_installed(self, fake_env, fake_systemd, capsys):
        rc = watch_mod.watch_uninstall()
        assert rc == 0
        assert "未安装" in capsys.readouterr().out


class TestWatchStatus:
    def test_status_active_shows_real_state(self, fake_env, monkeypatch, capsys):
        hermes_home = fake_env[1]
        # inbox 有 2 条未处理
        inbox = hermes_home / "watch" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text(
            json.dumps({"collected_at": "2026-08-12T10:00:00Z", "alerts": [], "processed": False}),
            encoding="utf-8",
        )
        (inbox / "b.json").write_text(
            json.dumps({"collected_at": "2026-08-12T10:01:00Z", "alerts": [], "processed": True}),
            encoding="utf-8",
        )
        (hermes_home / "watch" / "last_collect").write_text("2026-08-12T10:05:00Z", encoding="utf-8")
        monkeypatch.setattr(watch_mod, "_service_is_active", lambda: True)
        rc = watch_mod.watch_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "运行中" in out
        assert "上次采集: 2026-08-12T10:05:00Z" in out
        assert "inbox 未处理: 1 条" in out

    def test_status_inactive_warns(self, fake_env, monkeypatch, capsys):
        monkeypatch.setattr(watch_mod, "_service_is_active", lambda: False)
        rc = watch_mod.watch_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "未运行" in out
        assert "不会自动采集" in out

    def test_status_not_installed(self, fake_env, monkeypatch, capsys):
        monkeypatch.setattr(watch_mod, "_service_is_active", lambda: None)
        rc = watch_mod.watch_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "未安装" in out
        assert "vigil watch install" in out

    def test_watch_service_active_helper(self, fake_env, monkeypatch):
        # None = 未安装/无法判定（不许报假健康）
        monkeypatch.setattr(watch_mod, "unit_path", lambda: Path("/nonexistent/vigil-watch.service"))
        assert watch_mod.watch_service_active() is None
        # 安装了但 inactive → False
        unit = Path(fake_env[0]) / ".config" / "systemd" / "user" / "vigil-watch.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(watch_mod, "unit_path", lambda: unit)
        monkeypatch.setattr(
            watch_mod, "_systemctl",
            lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="inactive\n", stderr=""),
        )
        assert watch_mod.watch_service_active() is False
        # active → True
        monkeypatch.setattr(
            watch_mod, "_systemctl",
            lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="active\n", stderr=""),
        )
        assert watch_mod.watch_service_active() is True
