"""批次二十一 §Q 收口 — 凭据缺失 fail-closed（禁止自探测）。

覆盖：无 SSH 凭据（无 key/无密码/无 vault 引用）→ 直接报"未配置凭据"错误，
零 ssh 调用；提供 key_path 后正常执行；CLI 交互收集的 askpass 凭据正常；
prompt 常量含"凭据缺失 → 询问用户"行为约束。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tools.topo_discovery as topodisc
from tools.topo_discovery import DiscoveryError, _build_ssh_runner, discover_host


@pytest.fixture(autouse=True)
def _reset_breaker():
    topodisc._SSH_AUTH_FAILURES.clear()
    yield
    topodisc._SSH_AUTH_FAILURES.clear()


def test_no_credential_fails_closed_zero_ssh_calls(monkeypatch):
    """无凭据 → 报"未配置凭据"，不尝试默认 key/agent（零 ssh 调用）。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    with pytest.raises(DiscoveryError) as ei:
        _build_ssh_runner("203.0.113.50", "root")
    msg = str(ei.value)
    assert "未配置" in msg and "凭据" in msg
    assert "topo_update 补充" in msg
    assert "禁止自行翻 ~/.ssh/" in msg
    assert "询问用户提供正确凭据" in msg
    assert calls == [], "未配置凭据时不得发起任何 ssh 调用"


def test_discover_remote_host_no_credential_fails_closed(monkeypatch):
    """discover_host 远端 host 无凭据 → 同样 fail-closed（经 _build_ssh_runner）。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    with pytest.raises(DiscoveryError, match="未配置"):
        discover_host("203.0.113.51", "prod")
    assert calls == []


def test_key_path_credential_still_works(monkeypatch):
    """显式 key_path（拓扑 credential / --key）→ 正常 SSH 执行。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.52", "root", key_path="/keys/node.pem")
    assert runner("uptime").ok is True
    assert calls and calls[0][0] == "ssh"
    assert "-i" in calls[0] and "/keys/node.pem" in calls[0]


def test_askpass_credential_still_works(tmp_path, monkeypatch):
    """askpass 密码凭据（CLI 交互收集）→ 正常 SSH 执行（SSH_ASKPASS 注入）。"""
    vault = tmp_path / "vault"
    vault.write_text("pw", encoding="utf-8")
    askpass = topodisc._make_askpass_script(vault)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.53", "root", askpass_file=askpass)
    assert runner("uptime").ok is True
    assert calls and calls[0][0] == "ssh"
    assert "BatchMode=yes" not in calls[0]


def test_sudo_password_alone_is_not_ssh_credential(tmp_path, monkeypatch):
    """sudo_password_file 是 sudo 密码不是 SSH 认证凭据——单独提供仍 fail-closed。"""
    vault = tmp_path / "vault"
    vault.write_text("sudopw", encoding="utf-8")
    askpass = topodisc._make_askpass_script(vault)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    with pytest.raises(DiscoveryError, match="未配置"):
        _build_ssh_runner("203.0.113.54", "root", sudo_password_file=askpass)
    assert calls == []
