"""批四十六 — dashboard restart 端口继承真修（OPS-DELTA #63）。

§BS（2026-08-19 第 3 次复现，§AX 8/17 → §BF 8/18 → §BS 8/19）：``vigil
dashboard restart`` 仍默认 9119，不读 systemd 的 9120，只停不启；错误提示
引导 9119。

根因：restart parser 的 ``--port`` 默认值 = 9119，把"未传"伪装成"显式
9119"，``_resolve_dashboard_port`` 的 config/unit 回退永不执行（无法区分
"用户没传 --port" 与 "用户显式传了 9119"）。

本批改动（本文件逐条钉死）：
  1. parser ``--port`` 默认值 → ``None``：只有用户显式传了才是非 None；
     ``_resolve_dashboard_port`` 以 ``is not None`` 判定"显式"，显式 0
     （OS 自动分配）也原样透传；
  2. config 读改为 presence-sensitive（raw 文件，不合并 schema 默认 9119）：
     用户 config 没写 ``dashboard.port`` 时回退 unit ExecStart 端口（§BS
     场景：unit 9120 + config 无键 → 9120，而不是 schema 默认 9119）；
  3. restart = stop + spawn 分离的 dashboard server 子进程（继承端口；
     server 形态 cmdline 可被后续 restart / ``vigil dashboard stop`` 扫到）；
     失败提示带解析出的正确端口，绝不写死 9119。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.main import cmd_dashboard_restart


def _ns(**kw):
    defaults = dict(
        port=None, host="127.0.0.1", no_open=False, insecure=False,
        skip_build=False, isolated=False, open_profile="",
        stop=False, status=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _build_parser():
    """Real argparse tree with all dashboard subcommands wired to recorders."""
    from hermes_cli.subcommands.dashboard import build_dashboard_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    handlers = {}

    def _record(key):
        def _h(args):
            handlers[key] = args
        return _h

    build_dashboard_parser(
        sub,
        cmd_dashboard=_record("dash"),
        cmd_dashboard_register=_record("register"),
        cmd_dashboard_install=_record("install"),
        cmd_dashboard_uninstall=_record("uninstall"),
        cmd_dashboard_status=_record("status"),
        cmd_dashboard_stop=_record("stop"),
        cmd_dashboard_restart=_record("restart"),
    )
    return parser, handlers


class TestParserDistinguishesUnsetFromExplicit:
    """parser 默认 None：未传 --port ≠ 显式传 9119/0。"""

    @pytest.mark.parametrize(
        "argv",
        [
            ["dashboard"],
            ["dashboard", "start"],
            ["dashboard", "restart"],
            ["serve"],
            ["dashboard", "install"],
        ],
    )
    def test_no_port_flag_parses_to_none(self, argv):
        parser, _ = _build_parser()
        assert parser.parse_args(argv).port is None

    def test_explicit_port_still_parses(self):
        parser, _ = _build_parser()
        assert parser.parse_args(["dashboard", "restart", "--port", "9119"]).port == 9119
        assert parser.parse_args(["dashboard", "start", "--port", "0"]).port == 0
        assert parser.parse_args(["dashboard", "install", "--port", "9120"]).port == 9120


class TestConfigDashboardPortPresenceSensitive:
    """config 读是 presence-sensitive：没写键 → None（不拿 schema 默认 9119 顶）。"""

    def test_missing_key_returns_none(self, monkeypatch, tmp_path):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  theme: default\n", encoding="utf-8"
        )
        assert svc._config_dashboard_port() is None

    def test_explicit_key_returns_value(self, monkeypatch, tmp_path):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  port: 9120\n", encoding="utf-8"
        )
        assert svc._config_dashboard_port() == 9120

    def test_no_config_file_returns_none(self, monkeypatch, tmp_path):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        assert svc._config_dashboard_port() is None


class TestResolutionChainThreeStates:
    """真实 config 文件 + 真实 unit 文件：三态 + 显式传值。"""

    @pytest.fixture
    def svc(self, monkeypatch, tmp_path):
        from hermes_cli import dashboard_service as svc

        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        unit = tmp_path / "vigil-dashboard.service"
        monkeypatch.setattr(svc, "UNIT_PATH", unit)
        return svc, unit

    def _write_unit(self, unit: Path, port: int):
        unit.write_text(
            "[Service]\n"
            f"ExecStart=/opt/vigil/venv/bin/python -m hermes_cli.main dashboard "
            f"--no-open --skip-build --port {port}\n",
            encoding="utf-8",
        )

    def test_no_port_inherits_unit_9120_when_config_lacks_key(self, svc, tmp_path):
        svc_mod, unit = svc
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  theme: default\n", encoding="utf-8"
        )
        self._write_unit(unit, 9120)
        assert svc_mod._resolve_dashboard_port(None) == 9120

    def test_no_port_uses_config_value_when_set(self, svc, tmp_path):
        svc_mod, unit = svc
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  port: 9121\n", encoding="utf-8"
        )
        self._write_unit(unit, 9120)
        assert svc_mod._resolve_dashboard_port(None) == 9121

    def test_no_port_defaults_9119_when_no_sources(self, svc, tmp_path):
        svc_mod, unit = svc
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  theme: default\n", encoding="utf-8"
        )
        assert svc_mod._resolve_dashboard_port(None) == 9119

    def test_explicit_9119_does_not_fall_back(self, svc, tmp_path):
        svc_mod, unit = svc
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  port: 9121\n", encoding="utf-8"
        )
        self._write_unit(unit, 9120)
        assert svc_mod._resolve_dashboard_port(9119) == 9119

    def test_explicit_zero_passes_through(self, svc, tmp_path):
        svc_mod, unit = svc
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  port: 9121\n", encoding="utf-8"
        )
        self._write_unit(unit, 9120)
        assert svc_mod._resolve_dashboard_port(0) == 0


class TestRestartInheritsUnitPort:
    """restart 不带 --port → spawn 的 server 用继承端口（unit 9120），不是 9119。"""

    def test_restart_spawn_argv_carries_inherited_unit_port(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  theme: default\n", encoding="utf-8"
        )

        from hermes_cli import dashboard_service as svc

        unit = tmp_path / "vigil-dashboard.service"
        unit.write_text(
            "[Service]\n"
            "ExecStart=/opt/vigil/venv/bin/python -m hermes_cli.main dashboard "
            "--no-open --skip-build --port 9120\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(svc, "UNIT_PATH", unit)

        fake_proc = type("P", (), {"pid": 4242, "poll": lambda self: None})()
        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes"), \
             patch("hermes_cli.main.subprocess.Popen") as mock_popen, \
             patch("hermes_cli.main._wait_for_dashboard_ready", return_value=True):
            mock_popen.return_value = fake_proc
            rc = cmd_dashboard_restart(_ns())
        assert rc == 0
        argv = mock_popen.call_args.args[0]
        assert argv[argv.index("--port") + 1] == "9120"
        # server 形态：不是 restart lifecycle，后续 stop/restart 能扫到。
        assert "restart" not in argv
        assert argv[argv.index("-m") + 1] == "hermes_cli.main"
        assert "dashboard" in argv
        out = capsys.readouterr().out
        assert "http://127.0.0.1:9120" in out

    def test_restart_failure_hint_uses_inherited_port(
        self, monkeypatch, tmp_path, capsys
    ):
        """start 失败（端口占用）→ 错误提示引导继承端口，绝不回落 9119。"""
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "dashboard:\n  port: 9120\n", encoding="utf-8"
        )
        fake_proc = type("P", (), {"pid": 4242, "poll": lambda self: 1})()
        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]), \
             patch("hermes_cli.main._kill_stale_dashboard_processes"), \
             patch("hermes_cli.main.subprocess.Popen", return_value=fake_proc), \
             patch("hermes_cli.main._wait_for_dashboard_ready", return_value=False), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard_restart(_ns())
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "stopped but failed to start" in err
        assert "--port 9120" in err
        assert "--port 9119" not in err


class TestWaitForDashboardReady:
    """READY 行是"我们的 server 真的绑上了端口"的权威信号。

    占坑第三方（失败路径）会应答 TCP 探测，但不会打印 READY 行；上次运行遗留
    的 READY 行（start_size 之前的内容）也不算数。
    """

    def _proc(self, alive=True):
        return type("P", (), {"pid": 4242, "poll": lambda self: None if alive else 1})()

    def test_ready_line_appended_after_spawn_wins(self, monkeypatch, tmp_path):
        import hermes_cli.main as main_mod

        log = tmp_path / "restart.log"
        log.write_text("old VIGIL_DASHBOARD_READY port=9120\n", encoding="utf-8")
        calls = {"n": 0}

        def _sleep(_s):
            calls["n"] += 1
            if calls["n"] == 1:
                with open(log, "a", encoding="utf-8") as f:
                    f.write("VIGIL_DASHBOARD_READY port=9120\n")

        monkeypatch.setattr(main_mod._time, "sleep", _sleep)
        assert main_mod._wait_for_dashboard_ready(self._proc(), log, 9120) is True
        assert calls["n"] == 1

    def test_stale_ready_line_before_spawn_does_not_count(self, monkeypatch, tmp_path):
        import hermes_cli.main as main_mod

        log = tmp_path / "restart.log"
        log.write_text("VIGIL_DASHBOARD_READY port=9120\n", encoding="utf-8")
        monkeypatch.setattr(main_mod._time, "sleep", lambda _s: None)
        # 内容全部在 start_size 之前：没有新 READY → 超时 False（sleep 空转）。
        assert main_mod._wait_for_dashboard_ready(self._proc(), log, 9120, timeout=0.01) is False

    def test_child_exit_bails_early_without_ready_line(self, monkeypatch, tmp_path):
        import hermes_cli.main as main_mod

        log = tmp_path / "restart.log"
        log.write_text("", encoding="utf-8")
        monkeypatch.setattr(main_mod._time, "sleep", lambda _s: None)
        # 子进程已退出（bind 失败）→ 立即 False，不等超时。
        assert main_mod._wait_for_dashboard_ready(self._proc(alive=False), log, 9120) is False

    def test_third_party_listener_without_ready_line_fails(self, monkeypatch, tmp_path):
        """占坑第三方：TCP 可连但无 READY 行 → 不算成功（超时 False）。"""
        import hermes_cli.main as main_mod

        log = tmp_path / "restart.log"
        log.write_text("", encoding="utf-8")
        monkeypatch.setattr(main_mod._time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            main_mod, "_dashboard_listening", lambda host, port: True
        )
        # 即使 _dashboard_listening 为 True，没有 READY 行仍失败——正是失败路径
        # 需要的语义（我们的子进程没绑上，别人的监听不算数）。
        assert main_mod._wait_for_dashboard_ready(self._proc(), log, 9120, timeout=0.01) is False
