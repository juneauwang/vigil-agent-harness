"""批次四十（§AG）：``vigil dashboard start|stop|restart|status`` 子命令统一。

覆盖：
  - 四个子命令经真实 argparse 树注册（start/stop/restart/status）；
  - ``--stop`` flag 兼容保留（deprecated 仅 help 标注，行为不变）；
  - ``stop`` 子命令与 ``--stop`` 走同一 kill 路径（reason 带 stop）；
  - ``restart`` = stop + start（无进程时直接 start；stop 失败中止）；
  - ``status`` 输出含 PID/端口/启动时间/最近活跃（与 UI 停止按钮联动排查）；
  - 关键安全不变量：stop 分支永不落入 server-start。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.main import (
    cmd_dashboard,
    cmd_dashboard_restart,
    cmd_dashboard_status,
    cmd_dashboard_stop,
)


def _ns(**kw):
    defaults = dict(
        port=9119, host="127.0.0.1", no_open=False, insecure=False,
        stop=False, status=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestSubcommandWiring:
    """真实 argparse 树：四个生命周期子命令存在且分发到正确 handler。"""

    def _build_parser(self, handlers: dict):
        from hermes_cli.subcommands.dashboard import build_dashboard_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")

        def _record(key):
            def _h(args):
                handlers[key] = args
            return _h

        build_dashboard_parser(
            sub,
            cmd_dashboard=_record("start"),
            cmd_dashboard_register=_record("register"),
            cmd_dashboard_stop=_record("stop"),
            cmd_dashboard_restart=_record("restart"),
            cmd_dashboard_status=_record("status"),
        )
        return parser

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["dashboard", "start", "--port", "9123"], "start"),
            (["dashboard", "stop"], "stop"),
            (["dashboard", "restart", "--no-open"], "restart"),
            (["dashboard", "status"], "status"),
        ],
    )
    def test_subcommands_dispatch(self, argv, expected):
        handlers = {}
        parser = self._build_parser(handlers)
        ns = parser.parse_args(argv)
        assert ns.func is not None
        ns.func(ns)
        assert handlers[expected] is not None

    def test_start_accepts_server_runtime_flags(self):
        handlers = {}
        parser = self._build_parser(handlers)
        ns = parser.parse_args(
            ["dashboard", "start", "--port", "9123", "--host", "0.0.0.0",
             "--no-open", "--skip-build"]
        )
        assert ns.port == 9123
        assert ns.host == "0.0.0.0"
        assert ns.no_open is True
        assert ns.skip_build is True

    def test_stop_flag_still_parses_after_deprecation(self):
        handlers = {}
        parser = self._build_parser(handlers)
        ns = parser.parse_args(["dashboard", "--stop"])
        assert ns.stop is True

    def test_status_flag_still_parses(self):
        handlers = {}
        parser = self._build_parser(handlers)
        ns = parser.parse_args(["dashboard", "--status"])
        assert ns.status is True


class TestStopSubcommand:
    def test_stop_kills_and_exits_zero(self, capsys):
        scans = iter([[12345], []])
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=lambda: next(scans)), \
             patch("hermes_cli.main._kill_stale_dashboard_processes") as mock_kill, \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_stop(_ns())
        assert exc.value.code == 0
        kwargs = mock_kill.call_args.kwargs
        assert "stop" in kwargs["reason"].lower()

    def test_stop_no_processes_exits_zero(self, capsys):
        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_stop(_ns())
        assert exc.value.code == 0
        assert "No vigil dashboard processes running" in capsys.readouterr().out

    def test_stop_never_falls_through_to_server_start(self):
        """安全不变量：stop 分支永不落入 server-start（--stop 时代同款）。"""
        called = {"start": False}

        def fake_start_server(**kw):
            called["start"] = True

        fake_ws = argparse.Namespace(start_server=fake_start_server)
        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]), \
             patch.dict(sys.modules, {"hermes_cli.web_server": fake_ws}), \
             pytest.raises(SystemExit):
            cmd_dashboard(_ns(stop=True))
        assert called["start"] is False


class TestRestartSubcommand:
    def test_restart_stops_then_starts(self, capsys):
        """有进程 → 先 stop，再重入 cmd_dashboard（server-start 路径）。"""
        started = []

        def fake_cmd_dashboard(args):
            started.append(args)
            raise SystemExit(0)

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=[[12345], []]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes") as mock_kill, \
             patch("hermes_cli.main.cmd_dashboard", side_effect=fake_cmd_dashboard), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_restart(_ns())
        assert exc.value.code == 0
        mock_kill.assert_called_once()
        assert "restart" in mock_kill.call_args.kwargs["reason"].lower()
        assert len(started) == 1  # stop 后重入 start

    def test_restart_without_running_starts_fresh(self, capsys):
        """无进程 → 不 kill，直接 start（对齐 systemctl restart 停态语义）。"""
        started = []

        def fake_cmd_dashboard(args):
            started.append(args)
            raise SystemExit(0)

        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes") as mock_kill, \
             patch("hermes_cli.main.cmd_dashboard", side_effect=fake_cmd_dashboard), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_restart(_ns())
        assert exc.value.code == 0
        mock_kill.assert_not_called()
        assert "starting fresh" in capsys.readouterr().out
        assert len(started) == 1

    def test_restart_aborts_when_stop_leaves_survivors(self, capsys):
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=[[12345], [12345]]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes"), \
             patch("hermes_cli.main.cmd_dashboard") as mock_start, \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_restart(_ns())
        assert exc.value.code == 1
        assert "aborting restart" in capsys.readouterr().err
        mock_start.assert_not_called()


class TestStatusSubcommand:
    def test_status_output_contains_pid_and_port(self, capsys):
        processes = [
            (12345, "vigil dashboard --port 9120 --host 0.0.0.0"),
        ]
        with patch("hermes_cli.main._scan_dashboard_processes", return_value=processes), \
             patch("gateway.status._pid_exists", return_value=True), \
             patch("hermes_cli.main._dashboard_listening", return_value=True), \
             patch("hermes_cli.main._process_start_time", return_value="Mon Aug 17 09:00:00 2026"), \
             patch("hermes_cli.main._host_last_activity", return_value="2026-08-17 09:30:00"), \
             patch("hermes_cli.dashboard_service.UNIT_PATH",
                   Path("/nonexistent/vigil-dashboard.service")), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_status(_ns())
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "PID 12345" in out
        assert "port: 9120" in out
        assert "started" in out
        assert "last host activity" in out

    def test_status_reports_none_running(self, capsys):
        with patch("hermes_cli.main._scan_dashboard_processes", return_value=[]), \
             patch("hermes_cli.dashboard_service.UNIT_PATH",
                   Path("/nonexistent/vigil-dashboard.service")), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_status(_ns())
        assert exc.value.code == 0
        assert "No vigil dashboard processes running" in capsys.readouterr().out
