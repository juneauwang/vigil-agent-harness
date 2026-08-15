"""``vigil cron status`` 假健康修复（OPS-DELTA #12 缺陷 4/5 收尾）测试。

覆盖：
  - 心跳新鲜 → "✓ ticker 活跃，调度正常"；心跳缺失/过期 → 明确告警；
  - upstream Vigil 的 gateway PID 存在但非 Vigil 归属 → 不误报 running；
  - ``_pid_belongs_to_home`` 按 VIGIL_HOME env / 命令行归属判定；
  - vigil-watch 采集服务在跑 → 显示常驻接管提示。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cron.jobs as jobs
from hermes_cli import cron as cron_cli
from hermes_cli.cron import _pid_belongs_to_home


@pytest.fixture
def fake_status(monkeypatch):
    """把 cron_status 的外部依赖全部钉住（builtin provider + 无 job）。"""
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr(cron_cli, "_vigil_owned_gateway_pids", lambda: [])
    monkeypatch.setattr(jobs, "list_jobs", lambda **k: [])
    monkeypatch.setattr(
        "hermes_cli.watch.watch_service_active", lambda: None
    )

    def _set_heartbeat(hb=None, ok=None, last_error=None):
        monkeypatch.setattr(jobs, "get_ticker_heartbeat_age", lambda: hb)
        monkeypatch.setattr(jobs, "get_ticker_success_age", lambda: ok)
        monkeypatch.setattr(jobs, "get_ticker_last_error", lambda: last_error)

    return _set_heartbeat


class TestCronStatusHeartbeat:
    def test_fresh_heartbeat_reports_active(self, fake_status, capsys):
        fake_status(hb=30.0, ok=30.0)  # < 2×60s = 新鲜
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "Ticker: ✓ ticker 活跃，调度正常" in out
        assert "ticker 未运行" not in out

    def test_missing_heartbeat_warns(self, fake_status, capsys):
        fake_status(hb=None, ok=None)
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "Ticker: ⚠ ticker 未运行" in out
        assert "心跳缺失" in out
        assert "vigil-watch" in out

    def test_stale_heartbeat_warns(self, fake_status, capsys):
        fake_status(hb=9999.0, ok=None)
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "Ticker: ⚠ ticker 未运行" in out
        assert "9999" in out

    def test_fresh_heartbeat_but_failing_ticks_surfaces_error(self, fake_status, capsys):
        fake_status(hb=5.0, ok=9999.0, last_error="RuntimeError: boom")
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "可能每轮失败" in out
        assert "Last tick error: RuntimeError: boom" in out


class TestCronStatusOwnership:
    def test_upstream_pid_not_owned_not_running(self, fake_status, monkeypatch, capsys):
        # find_gateway_pids 会扫到 upstream 的 PID（hermes-gateway.service），
        # 但归属过滤后为空 → 不得报 "Gateway: 运行中"。
        monkeypatch.setattr(
            cron_cli, "_vigil_owned_gateway_pids", lambda: []
        )
        fake_status(hb=30.0, ok=30.0)
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "Gateway: ✗ 未运行" in out
        assert "Gateway: ✓" not in out

    def test_owned_pid_shows_running(self, fake_status, monkeypatch, capsys):
        monkeypatch.setattr(cron_cli, "_vigil_owned_gateway_pids", lambda: [4242])
        fake_status(hb=30.0, ok=30.0)
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "Gateway: ✓ 运行中 (PID 4242)" in out


class TestWatchTakeoverHint:
    def test_watch_active_hint(self, fake_status, monkeypatch, capsys):
        monkeypatch.setattr("hermes_cli.watch.watch_service_active", lambda: True)
        fake_status(hb=None, ok=None)
        cron_cli.cron_status()
        out = capsys.readouterr().out
        assert "采集已由 vigil-watch 常驻接管" in out


class TestPidBelongsToHome:
    def test_environ_match_returns_true(self, tmp_path, monkeypatch):
        home = tmp_path / "vigil"
        monkeypatch.setattr(
            cron_cli, "_read_process_environ",
            lambda pid: f"VIGIL_HOME={home}\x00OTHER=1",
        )
        assert _pid_belongs_to_home(12345, home) is True

    def test_environ_mismatch_returns_false(self, tmp_path, monkeypatch):
        home = tmp_path / "vigil"
        monkeypatch.setattr(
            cron_cli, "_read_process_environ",
            lambda pid: "VIGIL_HOME=/root/.hermes\x00OTHER=1",
        )
        assert _pid_belongs_to_home(12345, home) is False

    def test_unreadable_environ_falls_back_to_cmdline(self, tmp_path, monkeypatch):
        home = tmp_path / "vigil"
        monkeypatch.setattr(cron_cli, "_read_process_environ", lambda pid: None)
        monkeypatch.setattr(
            "gateway.status._read_process_cmdline",
            lambda pid: "python -m hermes_cli.gateway run --profile ops",
        )
        # ops profile 与 home 不匹配 → False（upstream/其它 profile 不算自己的）
        assert _pid_belongs_to_home(12345, home) is False

        monkeypatch.setattr(
            "gateway.status._read_process_cmdline",
            lambda pid: "python -m hermes_cli.gateway run -p ops",
        )
        assert _pid_belongs_to_home(12345, home) is False

    def test_both_unreadable_keeps_pid(self, monkeypatch):
        monkeypatch.setattr(cron_cli, "_read_process_environ", lambda pid: None)
        monkeypatch.setattr("gateway.status._read_process_cmdline", lambda pid: None)
        # 无法判定归属 → 保留（不误杀；宁可多报一个无法确认的 PID）
        assert _pid_belongs_to_home(999999, Path("/tmp/vigil")) is True
