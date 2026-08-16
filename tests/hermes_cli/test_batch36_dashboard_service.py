"""批三十六：``vigil dashboard install / uninstall / status``（systemd 常驻产品化）。

不碰真 systemd：monkeypatch ``_systemctl`` + ``UNIT_DIR`` 隔离。覆盖：unit
内容生成（真实 python 路径/HOME/端口参数化）；install 流程（写 unit →
daemon-reload → enable → start → URL）；重复 install 覆盖（先停旧服务）；
无 systemd user 会话明确报错不安装；uninstall 幂等（含 unit 不存在）；
status 摘要（未安装/已安装 + 端口解析）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import dashboard_service as svc


def _ok(stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stdout: str = "", stderr: str = "Failed to connect to bus") -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)


@pytest.fixture()
def unit(tmp_path, monkeypatch):
    """隔离 UNIT_DIR + launcher 解析（固定 python 路径便于断言）。"""
    monkeypatch.setattr(svc, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(svc, "UNIT_PATH", tmp_path / svc.UNIT_NAME)
    monkeypatch.setattr(
        svc, "_resolve_launcher", lambda: ("/opt/vigil/venv/bin/python", False)
    )
    return tmp_path


@pytest.fixture()
def fake_systemctl(monkeypatch):
    """可脚本化的 _systemctl：记录调用 + 按序/条件返回。"""
    calls: list = []

    def _make(calls, result_factory=lambda args: _ok()):
        def _run(*args, timeout=15):
            calls.append(list(args))
            return result_factory(args)

        return _run

    holder = {"calls": calls}
    monkeypatch.setattr(svc, "_systemctl", _make(calls))
    return holder


def test_unit_content_parameterized():
    content = svc._unit_content(9121, "/opt/vigil/venv/bin/python", "/home/u")
    assert "[Unit]" in content and "[Service]" in content and "[Install]" in content
    assert "Description=Vigil Dashboard - Web UI" in content
    assert "Environment=HOME=/home/u" in content
    assert "ExecStart=/opt/vigil/venv/bin/python -m hermes_cli.main dashboard --no-open --skip-build --port 9121" in content
    assert "Restart=on-failure" in content and "WantedBy=default.target" in content
    # 端口参数化
    assert "--port 9119" in svc._unit_content(9119, "python", "/home/u")
    assert "--port 8800" in svc._unit_content(8800, "python", "/home/u")


def test_resolve_launcher_prefers_sys_executable(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(svc.sys, "executable", str(exe))
    launcher, is_binary = svc._resolve_launcher()
    assert launcher == str(exe)
    assert is_binary is False


def test_resolve_launcher_falls_back_to_vigil(monkeypatch):
    monkeypatch.setattr(svc.sys, "executable", "")
    monkeypatch.setattr(svc.shutil, "which", lambda name: "/usr/local/bin/vigil" if name == "vigil" else None)
    launcher, is_binary = svc._resolve_launcher()
    assert launcher == "/usr/local/bin/vigil"
    assert is_binary is True
    assert svc._unit_exec_start(launcher, 9119, True).startswith(
        "/usr/local/bin/vigil dashboard --no-open --skip-build --port 9119"
    )


def test_install_writes_unit_and_starts(unit, fake_systemctl, capsys):
    rc = svc.cmd_dashboard_install(SimpleNamespace(port=9121))
    assert rc == 0
    unit_file = unit / svc.UNIT_NAME
    assert unit_file.is_file()
    content = unit_file.read_text(encoding="utf-8")
    assert "Environment=HOME=" in content
    assert "ExecStart=/opt/vigil/venv/bin/python -m hermes_cli.main dashboard --no-open --skip-build --port 9121" in content
    # 调用序列：show-environment(探测) + daemon-reload/enable/start
    calls = fake_systemctl["calls"]
    assert calls[0] == ["show-environment"]
    assert calls[-3:] == [["daemon-reload"], ["enable", "vigil-dashboard.service"], ["start", "vigil-dashboard.service"]]
    out = capsys.readouterr().out
    assert "http://127.0.0.1:9121" in out


def test_install_overwrites_existing_active_unit(unit, fake_systemctl, capsys, monkeypatch):
    """重复 install：先停旧服务（is-active=active → stop）再写新 unit。"""
    calls: list = []

    def _run(*args, timeout=15):
        calls.append(list(args))
        if args[0] == "is-active":
            return _ok("active\n")
        return _ok()

    monkeypatch.setattr(svc, "_systemctl", _run)
    rc = svc.cmd_dashboard_install(SimpleNamespace(port=9121))
    assert rc == 0
    assert ["stop", "vigil-dashboard.service"] in calls
    content = (unit / svc.UNIT_NAME).read_text(encoding="utf-8")
    assert "--port 9121" in content
    out = capsys.readouterr().out
    assert "已停止旧" in out
    assert "http://127.0.0.1:9121" in out


def test_install_no_systemd_errors_without_installing(unit, monkeypatch, capsys):
    monkeypatch.setattr(svc, "_systemctl", lambda *a, timeout=15: _fail(stderr="Failed to connect to bus: No such file or directory"))
    rc = svc.cmd_dashboard_install(SimpleNamespace(port=9121))
    assert rc == 1
    out = capsys.readouterr().out
    assert "当前环境无 systemd user 会话，无法常驻" in out
    assert not (unit / svc.UNIT_NAME).exists()  # 明确不安装


def test_install_systemctl_missing_errors(unit, monkeypatch, capsys):
    def _boom(*args, timeout=15):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(svc, "_systemctl", _boom)
    rc = svc.cmd_dashboard_install(SimpleNamespace(port=9121))
    assert rc == 1
    assert "当前环境无 systemd user 会话，无法常驻" in capsys.readouterr().out


def test_uninstall_idempotent_without_unit(unit, fake_systemctl, capsys):
    rc = svc.cmd_dashboard_uninstall(SimpleNamespace())
    assert rc == 0
    calls = fake_systemctl["calls"]
    assert ["stop", "vigil-dashboard.service"] in calls
    assert ["disable", "vigil-dashboard.service"] in calls
    assert ["daemon-reload"] in calls
    out = capsys.readouterr().out
    assert "不存在" in out  # unit 文件不存在 → 幂等成功


def test_uninstall_removes_unit(unit, fake_systemctl, capsys):
    (unit / svc.UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    rc = svc.cmd_dashboard_uninstall(SimpleNamespace())
    assert rc == 0
    assert not (unit / svc.UNIT_NAME).exists()
    out = capsys.readouterr().out
    assert "已删除" in out and "已卸载" in out


def test_status_not_installed(unit, fake_systemctl, capsys):
    rc = svc.cmd_dashboard_status(SimpleNamespace())
    assert rc == 0
    assert "未安装" in capsys.readouterr().out


def test_status_summary_with_port(unit, fake_systemctl, capsys):
    (unit / svc.UNIT_NAME).write_text(
        "[Service]\nExecStart=/opt/vigil/venv/bin/python -m hermes_cli.main dashboard --no-open --skip-build --port 9121\n",
        encoding="utf-8",
    )

    def _result_factory(args):
        if args[0] == "is-active":
            return _ok("active\n")
        if args[0] == "is-enabled":
            return _ok("enabled\n")
        return _ok()

    import hermes_cli.dashboard_service as _svc

    def _run(*args, timeout=15):
        return _result_factory(args)

    from unittest.mock import patch
    with patch.object(_svc, "_systemctl", side_effect=_run):
        rc = _svc.cmd_dashboard_status(SimpleNamespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "vigil-dashboard.service" in out
    assert "active" in out
    assert "enabled" in out
    assert "http://127.0.0.1:9121" in out
