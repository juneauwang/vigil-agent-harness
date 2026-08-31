"""批次十六 §AD — SSH 认证限流熔断验收测试（OPS-DELTA 批次十六 任务 3）。

覆盖：连续认证失败 3 次 → 熔断（第 4 次不再调用底层）；不同 host:user 独立
计数；成功一次后计数重置；非认证类 exit 255（连接拒绝）不计数；CLI 交互路径
（vssh）不经过本 runner，不计数不受影响。
"""

from __future__ import annotations

import datetime as _dt
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


def _age_breaker_entry(host: str, user: str, seconds: float) -> None:
    """把熔断条目的 ts 拨老 seconds 秒（模拟 TTL 过期）。"""
    key = topodisc._ssh_auth_key(host, user)
    entry = topodisc._SSH_AUTH_FAILURES.setdefault(key, {})
    entry["ts"] = (_dt.datetime.now() - _dt.timedelta(seconds=seconds)).isoformat()


def test_trips_after_three_failures_and_stops_calling_underlying(monkeypatch):
    """连续 exit 255 Permission denied ×3 → 第 4 次直接熔断错误、不再调用底层。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.20", "root", key_path="/keys/test.pem")

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
    runner = _build_ssh_runner("203.0.113.20", "root", key_path="/keys/test.pem")
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
    runner_a = _build_ssh_runner("host-a", "root", key_path="/keys/test.pem")
    runner_b = _build_ssh_runner("host-b", "root", key_path="/keys/test.pem")

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
    runner = _build_ssh_runner("203.0.113.21", "root", key_path="/keys/test.pem")

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
    runner = _build_ssh_runner("203.0.113.22", "root", key_path="/keys/test.pem")

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

# ---------------------------------------------------------------------------
# 批次十九 §AF — 熔断信号扩展 + 共享计数（sudo_tool 远端路径接入）
# ---------------------------------------------------------------------------

def test_too_many_auth_failures_signal_counts_even_without_255(monkeypatch):
    """MaxAuthTries 耗尽信号（stderr 含 Too many）不再要求 exit 255。

    个别 ssh 包装/ansible 返回码不是 255，但提示出现即 sshd 限流信号。
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        topodisc.subprocess, "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="Too many authentication failures for root"),
    )
    runner = _build_ssh_runner("203.0.113.30", "root", key_path="/keys/test.pem")
    with pytest.raises(DiscoveryError, match="SSH 连接"):
        runner("uptime")
    assert topodisc._ssh_auth_failures("203.0.113.30", "root") == 1


def test_permission_denied_twice_in_one_attempt_counts(monkeypatch):
    """单次尝试内 Permission denied 出现两次 = 多 key 遍历的认证失败信号。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        topodisc.subprocess, "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout="",
            stderr=("Permission denied (publickey).\n"
                    "Permission denied (publickey,password).")),
    )
    runner = _build_ssh_runner("203.0.113.31", "root", key_path="/keys/test.pem")
    with pytest.raises(DiscoveryError, match="SSH 连接"):
        runner("uptime")
    assert topodisc._ssh_auth_failures("203.0.113.31", "root") == 1


def test_breaker_error_includes_identities_only_and_layering():
    """熔断错误信息带分层归因：连接层 → 检查 IdentitiesOnly；执行层才换姿势。"""
    msg = topodisc._ssh_auth_breaker_error("203.0.113.32", "root")
    assert "IdentitiesOnly=yes" in msg
    assert "Too many authentication failures" in msg
    assert "连接层错误" in msg and "执行层错误" in msg
    assert "MaxAuthTries=6" in msg and "已停止自动重试" in msg


def test_sudo_tool_scp_ssh_paths_share_breaker_counter(monkeypatch):
    """sudo_exec 远端 scp/ssh 路径（§AF 三兄弟的 scp/ssh）接入共享熔断计数。

    mock ansible/scp 族路径：连续认证失败 3 次（scp 2 次 + ssh 1 次）→ 熔断
    错误（含 IdentitiesOnly 提示）；成功后计数清零——与 topo_discovery runner
    共用同一模块级计数（跨工具共享状态）。
    """
    import tools.sudo_tool as sudo_tool

    ssh_argv = ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "ops@host"]
    auth_fail = SimpleNamespace(returncode=255, stdout="",
                                stderr="Permission denied (publickey,password).")
    ok = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        # 前 3 次底层调用模拟认证失败（scp/ssh 任意），之后成功
        return auth_fail if len(calls) <= 3 else ok

    monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)

    from pathlib import Path
    # 第 1 次：scp 失败（计数 1）→ 抛 scp 上传失败
    with pytest.raises(RuntimeError, match="scp 上传失败"):
        sudo_tool._scp(ssh_argv, {}, Path("/local/f"), "/tmp/x")
    assert topodisc._ssh_auth_failures("host", "ops") == 1
    # 第 2 次：scp 失败（计数 2）
    with pytest.raises(RuntimeError, match="scp 上传失败"):
        sudo_tool._scp(ssh_argv, {}, Path("/local/f"), "/tmp/x")
    assert topodisc._ssh_auth_failures("host", "ops") == 2
    # 第 3 次：ssh 失败（计数 3 → 熔断），错误可操作
    with pytest.raises(RuntimeError, match="认证失败 3/3"):
        sudo_tool._ssh_run(ssh_argv, {}, "sudo -A uptime")
    # 第 4 次：入口直接熔断（不再调用底层），错误含 IdentitiesOnly 归因
    with pytest.raises(RuntimeError) as ei:
        sudo_tool._ssh_run(ssh_argv, {}, "sudo -A uptime")
    breaker_msg = str(ei.value)
    assert "已停止自动重试" in breaker_msg and "IdentitiesOnly=yes" in breaker_msg
    assert "连接层错误" in breaker_msg
    assert len(calls) == 3  # 底层只被调用 3 次
    # 成功后计数清零：凭据修复（清计数）后一次成功 ssh → 不再累积
    topodisc._SSH_AUTH_FAILURES.clear()
    monkeypatch.setattr(sudo_tool.subprocess, "run", lambda argv, **kw: ok)
    sudo_tool._ssh_run(ssh_argv, {}, "sudo -A uptime")
    assert topodisc._ssh_auth_failures("host", "ops") == 0


# ---------------------------------------------------------------------------
# 批次二十一 — 凭据 fail-closed 硬化（任务 1：熔断错误信息扩展）
# ---------------------------------------------------------------------------

def test_breaker_error_forbids_credential_self_probing():
    """熔断错误信息带 fail-closed 指令：禁止换用户名/换 key/翻 ~/.ssh/ 继续。

    §Q/§AD/§AF 实锤：agent 在熔断后换姿势/换凭据来源继续试，把主机锁 15
    分钟——错误信息必须显式禁止自探测并引导问用户。
    """
    topodisc._SSH_AUTH_FAILURES.clear()
    topodisc._record_ssh_auth_failure("203.0.113.40", "root")
    topodisc._record_ssh_auth_failure("203.0.113.40", "root")
    topodisc._record_ssh_auth_failure("203.0.113.40", "root")
    try:
        msg = topodisc._ssh_auth_breaker_error("203.0.113.40", "root")
        assert "请勿换用户名/换 key/翻 ~/.ssh/ 继续尝试" in msg
        assert "限流锁 15 分钟" in msg
        assert "询问用户提供正确凭据" in msg
    finally:
        topodisc._SSH_AUTH_FAILURES.clear()


# ---------------------------------------------------------------------------
# 批次二十一 — 凭据 fail-closed（任务 3：用户纠正 force_trip 联动）
# ---------------------------------------------------------------------------

def test_force_trip_immediately_breaks_with_zero_ssh_calls(monkeypatch):
    """force_trip 后该 host:user 立即熔断——入口直接报错，不再调用底层。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    topodisc.force_trip("203.0.113.60", "root")
    try:
        runner = _build_ssh_runner("203.0.113.60", "root", key_path="/keys/test.pem")
        with pytest.raises(DiscoveryError, match="已停止自动重试") as ei:
            runner("uptime")
        assert "询问用户提供正确凭据" in str(ei.value)
        assert calls == [], "force_trip 后不得发起任何 ssh 调用"
    finally:
        topodisc._SSH_AUTH_FAILURES.clear()


def test_force_trip_keeps_other_hosts_untouched(monkeypatch):
    """force_trip 只作用于目标 host:user，其他 host 计数不受影响。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    topodisc.force_trip("host-ft", "root")
    try:
        assert topodisc._ssh_auth_breaker_tripped("host-ft", "root") is True
        assert topodisc._ssh_auth_failures("host-other", "root") == 0
        runner = _build_ssh_runner("host-other", "root", key_path="/keys/test.pem")
        with pytest.raises(DiscoveryError, match="SSH 连接"):
            runner("uptime")
    finally:
        topodisc._SSH_AUTH_FAILURES.clear()


def test_force_trip_trips_sudo_tool_guard(monkeypatch):
    """sudo_tool 远端路径共享同一计数——force_trip 后 guard 直接熔断。"""
    import tools.sudo_tool as sudo_tool

    topodisc.force_trip("host-sudo", "ops")
    try:
        from tools.sudo_tool import _ssh_auth_breaker_guard
        with pytest.raises(RuntimeError, match="已停止自动重试"):
            _ssh_auth_breaker_guard(["ssh", "-p", "22", "ops@host-sudo"])
    finally:
        topodisc._SSH_AUTH_FAILURES.clear()


# ---------------------------------------------------------------------------
# 批八十四 — 熔断过期 + 凭据修正重试（OPS-DELTA #100 防死锁）
# ---------------------------------------------------------------------------

def test_breaker_immediate_retry_rejected_until_ttl(monkeypatch):
    """验收 d+e：3 次失败熔断 → 立即重试被拒（不调用底层）；超过 TTL 后自动
    放行一次真实 SSH 尝试（计数重新从 1 累计，不再需要重启进程）。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.70", "root", key_path="/keys/test.pem")
    for _ in range(3):
        with pytest.raises(DiscoveryError, match="SSH 连接|认证失败"):
            runner("uptime")
    # d：熔断期内立即重试被拒，底层零调用
    with pytest.raises(DiscoveryError, match="已停止自动重试"):
        runner("uptime")
    assert len(calls) == 3
    # e：TTL 过期 → 放行真实尝试，再次失败只累计 1（重新计时）
    _age_breaker_entry("203.0.113.70", "root",
                       topodisc._SSH_AUTH_BREAKER_TTL_S + 1)
    with pytest.raises(DiscoveryError, match="SSH 连接"):
        runner("uptime")
    assert len(calls) == 4
    assert topodisc._ssh_auth_failures("203.0.113.70", "root") == 1


def test_corrected_credentials_bypass_breaker_and_reset(monkeypatch):
    """验收 f：修正凭据（换 key）→ 放行一次真实 SSH 尝试，成功后计数清零
    （不再死锁，无需重启进程）。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "/keys/correct.pem" in argv:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner_bad = _build_ssh_runner("203.0.113.71", "root", key_path="/keys/bad.pem")
    for _ in range(3):
        with pytest.raises(DiscoveryError, match="SSH 连接|认证失败"):
            runner_bad("uptime")
    with pytest.raises(DiscoveryError, match="已停止自动重试"):
        runner_bad("uptime")
    assert len(calls) == 3
    # 修正凭据（换 key）→ 真实 SSH 尝试 + 成功清零
    runner_good = _build_ssh_runner("203.0.113.71", "root", key_path="/keys/correct.pem")
    assert runner_good("uptime").ok is True
    assert len(calls) == 4
    assert topodisc._ssh_auth_failures("203.0.113.71", "root") == 0


def test_failures_after_expiry_reaccumulate_and_retrip(monkeypatch):
    """验收 g：过期放行后连续失败仍累计 3 次重新熔断（MaxAuthTries 防自伤不破）。"""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _auth_fail()

    monkeypatch.setattr(topodisc.subprocess, "run", fake_run)
    runner = _build_ssh_runner("203.0.113.72", "root", key_path="/keys/test.pem")
    for _ in range(3):
        with pytest.raises(DiscoveryError):
            runner("uptime")
    _age_breaker_entry("203.0.113.72", "root",
                       topodisc._SSH_AUTH_BREAKER_TTL_S + 1)
    with pytest.raises(DiscoveryError, match="SSH 连接"):  # 过期放行后失败 1
        runner("uptime")
    with pytest.raises(DiscoveryError, match="SSH 连接"):  # 失败 2
        runner("uptime")
    with pytest.raises(DiscoveryError, match="认证失败 3/3"):  # 失败 3 → 再次熔断
        runner("uptime")
    with pytest.raises(DiscoveryError, match="已停止自动重试"):  # 又立即被拒
        runner("uptime")
    assert len(calls) == 6  # 3（首轮）+ 3（过期后重新累计）；熔断后的拒绝零调用


def test_sudo_tool_guard_allows_corrected_key_retry():
    """sudo_exec 远端路径共享计数：凭据修正（换 key）后 guard 放行并清零。"""
    import tools.sudo_tool as sudo_tool

    topodisc._record_ssh_auth_failure("host-fix", "ops", ("/keys/bad.pem",))
    topodisc._record_ssh_auth_failure("host-fix", "ops", ("/keys/bad.pem",))
    topodisc._record_ssh_auth_failure("host-fix", "ops", ("/keys/bad.pem",))
    try:
        with pytest.raises(RuntimeError, match="已停止自动重试"):
            sudo_tool._ssh_auth_breaker_guard(["ssh", "-i", "/keys/bad.pem", "ops@host-fix"])
        # 修正 key → 放行（不再 raise），计数清零
        sudo_tool._ssh_auth_breaker_guard(["ssh", "-i", "/keys/good.pem", "ops@host-fix"])
        assert topodisc._ssh_auth_failures("host-fix", "ops") == 0
    finally:
        topodisc._SSH_AUTH_FAILURES.clear()
