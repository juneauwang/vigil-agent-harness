"""批次十六 §AD — SSH 认证限流熔断验收测试（OPS-DELTA 批次十六 任务 3）。

覆盖：连续认证失败 3 次 → 熔断（第 4 次不再调用底层）；不同 host:user 独立
计数；成功一次后计数重置；非认证类 exit 255（连接拒绝）不计数；CLI 交互路径
（vssh）不经过本 runner，不计数不受影响。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import tools.topo_discovery as topodisc
from tools.topo_discovery import DiscoveryError, _build_ssh_runner


@pytest.fixture(autouse=True)
def _reset_breaker():
    topodisc._SSH_AUTH_FAILURES.clear()
    yield
    topodisc._SSH_AUTH_FAILURES.clear()


def _auth_fail(stderr: str = "Permission denied (publickey,password).") -> SimpleNamespace:
    return SimpleNamespace(returncode=255, stdout="", stderr=stderr)


def test_trips_after_three_failures_and_stops_calling_underlying(monkeypatch):
    """连续 exit 255 Permission denied ×3 → 第 4 次直接熔断错误、不再调用底层。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.20", "root")

    with pytest.raises(DiscoveryError, match="SSH 连接"):   # 第 1 次：普通认证失败
        runner("uptime")
    with pytest.raises(DiscoveryError, match="SSH 连接"):   # 第 2 次
        runner("uptime")
    with pytest.raises(DiscoveryError, match="认证失败 3/3"):  # 第 3 次：达上限 → 熔断消息
        runner("uptime")
    with pytest.raises(DiscoveryError, match="已停止自动重试"):  # 第 4 次：入口直接熔断
        runner("uptime")
    assert len(calls) == 3  # 底层只被调用 3 次


def test_breaker_message_is_actionable(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.20", "root")
    with pytest.raises(DiscoveryError, match="SSH 连接"):
        runner("uptime")
    with pytest.raises(DiscoveryError, match="SSH 连接"):
        runner("uptime")
    with pytest.raises(DiscoveryError) as ei:
        runner("uptime")
    msg = str(ei.value)
    assert "MaxAuthTries=6" in msg and "已停止自动重试" in msg
    assert "手动 ssh 验证凭据" in msg and "topo credential" in msg
    assert "root@203.0.113.20" in msg


def test_hosts_independent_counters(monkeypatch):
    """host A 熔断不影响 host B。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner_a = _build_ssh_runner("host-a", "root")
    runner_b = _build_ssh_runner("host-b", "root")

    for _ in range(3):
        with pytest.raises(DiscoveryError):
            runner_a("uptime")
    with pytest.raises(DiscoveryError, match="已停止自动重试"):
        runner_a("uptime")
    # host B 计数独立——仍调用底层，报普通认证失败
    with pytest.raises(DiscoveryError, match="SSH 连接"):
        runner_b("uptime")
    assert len(calls) == 4


def test_success_resets_counter(monkeypatch):
    """成功一次（非 255）说明凭据 OK → 计数清零，重新开始数。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 3:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.21", "root")

    with pytest.raises(DiscoveryError):  # 失败 1
        runner("uptime")
    with pytest.raises(DiscoveryError):  # 失败 2
        runner("uptime")
    assert runner("uptime").ok is True   # 成功 → 清零

    with pytest.raises(DiscoveryError):  # 重置后失败 1
        runner("uptime")
    with pytest.raises(DiscoveryError):  # 重置后失败 2
        runner("uptime")
    with pytest.raises(DiscoveryError, match="认证失败 3/3"):
        runner("uptime")                 # 重置后失败 3 → 熔断
    assert len(calls) == 6


def test_non_auth_255_not_counted(monkeypatch):
    """连接拒绝（非认证失败）不计数——不会累积成熔断。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=255, stdout="", stderr="Connection refused")

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.22", "root")

    for _ in range(5):
        with pytest.raises(DiscoveryError, match="SSH 连接"):
            runner("uptime")
    assert len(calls) == 5
    assert topodisc._ssh_auth_failures("203.0.113.22", "root") == 0


def test_cli_vssh_path_not_counted(monkeypatch):
    """CLI 交互路径（vssh._build_ssh_argv）不经过 runner，不计数不受影响。"""
    from hermes_cli.subcommands.vssh import _build_ssh_argv

    before = dict(topodisc._SSH_AUTH_FAILURES)
    argv, env = _build_ssh_argv("db1", user="root")
    assert argv == ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "root@db1"]
    assert topodisc._SSH_AUTH_FAILURES == before
