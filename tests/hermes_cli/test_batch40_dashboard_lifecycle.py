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
from types import SimpleNamespace
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
        """有进程 → 先 stop，再 spawn 分离的 dashboard server（继承 9120）。"""
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=[[12345], []]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes") as mock_kill, \
             patch("hermes_cli.main.subprocess.Popen") as mock_popen, \
             patch("hermes_cli.main._wait_for_dashboard_ready", return_value=True), \
             patch("hermes_cli.main._resolve_dashboard_port", return_value=9120):
            fake_proc = type("P", (), {"pid": 4242, "poll": lambda self: None})()
            mock_popen.return_value = fake_proc
            rc = cmd_dashboard_restart(_ns(port=None))
        assert rc == 0
        mock_kill.assert_called_once()
        assert "restart" in mock_kill.call_args.kwargs["reason"].lower()
        argv = mock_popen.call_args.args[0]
        assert "--port" in argv and argv[argv.index("--port") + 1] == "9120"
        # 子进程是 server 形态（dashboard），不是 lifecycle（restart）→ 下次
        # restart / `vigil dashboard stop` 能扫到并停掉它。
        assert "dashboard" in argv
        assert "restart" not in argv
        assert "--no-open" in argv and "--skip-build" in argv

    def test_restart_without_running_starts_fresh(self, capsys):
        """无进程 → 不 kill，直接 spawn（对齐 systemctl restart 停态语义）。"""
        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes") as mock_kill, \
             patch("hermes_cli.main.subprocess.Popen") as mock_popen, \
             patch("hermes_cli.main._wait_for_dashboard_ready", return_value=True), \
             patch("hermes_cli.main._resolve_dashboard_port", return_value=9120):
            fake_proc = type("P", (), {"pid": 4242, "poll": lambda self: None})()
            mock_popen.return_value = fake_proc
            rc = cmd_dashboard_restart(_ns(port=None))
        assert rc == 0
        mock_kill.assert_not_called()
        out = capsys.readouterr().out
        assert "starting fresh" in out
        assert "http://127.0.0.1:9120" in out

    def test_restart_aborts_when_stop_leaves_survivors(self, capsys):
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=[[12345], [12345]]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes"), \
             patch("hermes_cli.main.subprocess.Popen") as mock_popen, \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_restart(_ns())
        assert exc.value.code == 1
        assert "aborting restart" in capsys.readouterr().err
        mock_popen.assert_not_called()


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


class TestFindStaleDashboardPidsSelfExclusion:
    """batch 43 §AX：``vigil dashboard restart`` 不得把自己当 server SIGTERM。"""

    def test_restart_cmdline_is_not_treated_as_server(self):
        import os

        import hermes_cli.dashboard_procs as dp

        self_cmdline = "python -m hermes_cli.main dashboard restart --port 9119"
        server_cmdline = "python -m hermes_cli.main dashboard --port 9120 --no-open"
        real_run = dp.subprocess.run

        def _fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "ps":
                lines = [f"102 {server_cmdline}"]
                # The restart (self) process has this cmdline.
                lines.append(f"{os.getpid()} {self_cmdline}")
                return type("R", (), {"returncode": 0, "stdout": "\n".join(lines)})()
            return real_run(cmd, *a, **kw)

        with patch("hermes_cli.dashboard_procs.subprocess.run", side_effect=_fake_run):
            found = dp._scan_dashboard_processes()
        pids = [pid for pid, _c in found]
        # 真 server 进程在；restart（自身）进程绝不返回。
        assert 102 in pids
        assert os.getpid() not in pids

    def test_stop_and_status_cmdlines_excluded_too(self):
        import os

        import hermes_cli.dashboard_procs as dp

        real_run = dp.subprocess.run
        cmds = {
            os.getpid(): "python -m hermes_cli.main dashboard stop",
            os.getpid() + 1: "python -m hermes_cli.main dashboard status",
            205: "python -m hermes_cli.main dashboard --port 9120 --no-open",
        }

        def _fake_run(cmd, *a, **kw):
            if cmd and cmd[0] == "ps":
                lines = [f"{pid} {c}" for pid, c in cmds.items()]
                return type("R", (), {"returncode": 0, "stdout": "\n".join(lines)})()
            return real_run(cmd, *a, **kw)

        with patch("hermes_cli.dashboard_procs.subprocess.run", side_effect=_fake_run):
            found = dp._scan_dashboard_processes()
        pids = [pid for pid, _c in found]
        assert 205 in pids
        assert os.getpid() not in pids
        assert os.getpid() + 1 not in pids


class TestRestartStartFailureOutput:
    """batch 43 §AX + 批46 §BS：restart 的 start 阶段若失败，必须输出明确错误，
    且提示里的端口是解析继承的那个（不是硬编码 9119）。"""

    def test_restart_prints_failure_when_start_fails(self, capsys):
        """stop 成功但 spawn 的 server 起不来（端口占用等）→ 明确错误 + 正确端口。"""
        fake_proc = type("P", (), {"pid": 4242, "poll": lambda self: 1})()
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=[[12345], []]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes"), \
             patch("hermes_cli.main.subprocess.Popen", return_value=fake_proc), \
             patch("hermes_cli.main._wait_for_dashboard_ready", return_value=False), \
             patch("hermes_cli.main._resolve_dashboard_port", return_value=9120), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_restart(_ns(port=None))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "stopped but failed to start" in err
        # 提示引导继承端口 9120，绝不回落到 9119 默认。
        assert "--port 9120" in err

    def test_restart_start_success_no_extra_error(self, capsys):
        """start 成功（listening）→ 不输出失败提示。"""
        fake_proc = type("P", (), {"pid": 4242, "poll": lambda self: None})()
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   side_effect=[[12345], []]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes"), \
             patch("hermes_cli.main.subprocess.Popen", return_value=fake_proc), \
             patch("hermes_cli.main._wait_for_dashboard_ready", return_value=True), \
             patch("hermes_cli.main._resolve_dashboard_port", return_value=9120):
            rc = cmd_dashboard_restart(_ns(port=None))
        assert rc == 0
        err = capsys.readouterr().err
        assert "failed to start" not in err


class TestPortSingleSourceResolution:
    """batch 43 §BF + 批46 §BS：config dashboard.port 是端口单一事实来源；
    "未传 --port" 与 "显式 9119" 可区分（未传 → config/unit/默认 回退）。"""

    def test_resolve_priority_explicit_port_wins(self, monkeypatch):
        from hermes_cli import dashboard_service as svc

        # 显式 --port 优先于 config/unit。
        monkeypatch.setattr(svc, "_unit_port", lambda: 9999)
        monkeypatch.setattr(svc, "_config_dashboard_port", lambda: 9121)
        assert svc._resolve_dashboard_port(9130) == 9130

    def test_resolve_falls_back_to_config_port(self, monkeypatch):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setattr(svc, "_unit_port", lambda: 9999)
        monkeypatch.setattr(svc, "_config_dashboard_port", lambda: 9121)
        assert svc._resolve_dashboard_port(None) == 9121

    def test_resolve_falls_back_to_unit_when_no_config(self, monkeypatch):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setattr(svc, "_unit_port", lambda: 9120)
        monkeypatch.setattr(svc, "_config_dashboard_port", lambda: None)
        assert svc._resolve_dashboard_port(None) == 9120

    def test_resolve_default_when_no_source(self, monkeypatch):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setattr(svc, "_unit_port", lambda: 9119)
        monkeypatch.setattr(svc, "_config_dashboard_port", lambda: None)
        assert svc._resolve_dashboard_port(None) == 9119

    def test_resolve_explicit_9119_stays_9119(self, monkeypatch):
        """显式 --port 9119 与"未传"不同：不触发 config/unit 回退，就用 9119。"""
        from hermes_cli import dashboard_service as svc

        monkeypatch.setattr(svc, "_unit_port", lambda: 9120)
        monkeypatch.setattr(svc, "_config_dashboard_port", lambda: 9121)
        assert svc._resolve_dashboard_port(9119) == 9119

    def test_resolve_explicit_zero_passes_through(self, monkeypatch):
        """显式 --port 0（OS 自动分配）原样透传，不落入回退链。"""
        from hermes_cli import dashboard_service as svc

        monkeypatch.setattr(svc, "_unit_port", lambda: 9120)
        monkeypatch.setattr(svc, "_config_dashboard_port", lambda: 9121)
        assert svc._resolve_dashboard_port(0) == 0

    def test_install_persists_port_to_config_and_unit_agree(self, tmp_path, monkeypatch):
        """install --port 9120 → config dashboard.port=9120 且 unit ExecStart 同端口。"""
        import hermes_cli.dashboard_service as svc

        monkeypatch.setattr(svc, "UNIT_DIR", tmp_path)
        monkeypatch.setattr(svc, "UNIT_PATH", tmp_path / svc.UNIT_NAME)
        monkeypatch.setattr(
            svc, "_resolve_launcher", lambda: ("/opt/vigil/venv/bin/python", False)
        )
        monkeypatch.setattr(svc, "_systemd_user_available", lambda: (True, ""))

        calls: list = []

        def _run(*args, timeout=15):
            calls.append(list(args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(svc, "_systemctl", _run)

        written = {}
        saved = {}

        def _load():
            return saved

        def _save(cfg):
            written["cfg"] = dict(cfg)

        monkeypatch.setattr(svc, "load_config", _load)
        monkeypatch.setattr(svc, "save_config", _save)
        rc = svc.cmd_dashboard_install(SimpleNamespace(port=9120))
        assert rc == 0
        # config 落 dashboard.port
        assert written["cfg"]["dashboard"]["port"] == 9120
        # unit ExecStart 同端口
        content = (tmp_path / svc.UNIT_NAME).read_text(encoding="utf-8")
        assert "--port 9120" in content
