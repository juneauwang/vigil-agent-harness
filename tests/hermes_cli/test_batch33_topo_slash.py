"""批三十三 T2b/T2c — /topo 逗号分隔 host + 无参用法提示。

覆盖：host 参数 `ip1,ip2` 拆成两个主机分别发现；交互模式打印用法行；
交互输入也支持逗号拆分。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml

from cli import HermesCLI
import hermes_cli.config as hc


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.config = {}
    cli_obj.console = MagicMock()
    cli_obj.agent = None
    cli_obj.conversation_history = []
    cli_obj.session_id = "session-123"
    cli_obj._app = None
    cli_obj.compact = True
    cli_obj.model = "gpt-5"
    cli_obj.provider = "openai"
    cli_obj.enabled_toolsets = []
    cli_obj._session_db = None
    cli_obj._battery_visible = False
    cli_obj.show_banner = lambda: None
    return cli_obj


def _printed(cli_obj) -> str:
    return "\n".join(str(c.args[0]) for c in cli_obj.console.print.call_args_list)


def _discovery(host: str = "203.0.113.20", env: str = "prod"):
    return {
        "version": 3, "source": "discovered", "last_verified": "2026-08-14",
        "needs_review": True,
        "host": {"name": host, "env": env, "cluster": "default", "endpoint": host,
                 "runtime": "docker", "source": "discovered", "needs_review": True,
                 "last_verified": "2026-08-14", "services_index": f"hosts/{host}.yaml"},
        "services": [
            {"name": "app", "type": "service", "env": env, "cluster": "default",
             "endpoint": f"{host}:8080", "source": "discovered",
             "last_verified": "2026-08-14", "needs_review": True,
             "detail": f"entities/prod__{host}__app.yaml", "attrs": {}},
        ],
        "details": {}, "pending_review": [], "probes": {"docker": "ok", "ss": "ok"},
    }


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _patch_discover_recording(monkeypatch):
    import tools.topo_discovery as td
    seen = []

    def fake_discover(host, env, creds, *, cluster=""):
        seen.append(host)
        return _discovery(host, env)

    monkeypatch.setattr(td, "discover_host", fake_discover)
    return seen


def _patch_cred_resolve(monkeypatch, cred):
    import hermes_cli.subcommands.vssh as vssh
    monkeypatch.setattr(vssh, "_resolve_topology_credential", lambda host: cred)


def test_topo_comma_separated_hosts_discovered_individually(topo_home, monkeypatch):
    seen = _patch_discover_recording(monkeypatch)
    _patch_cred_resolve(monkeypatch, {"type": "ssh_key", "ref": "/k", "user": "root"})
    cli_obj = _make_cli()
    assert cli_obj.process_command("/topo 203.0.113.20,203.0.113.21 --env prod --yes") is True
    assert seen == ["203.0.113.20", "203.0.113.21"]
    out = _printed(cli_obj)
    assert "203.0.113.20" in out
    assert "203.0.113.21" in out


def test_topo_comma_with_spaces_and_options(topo_home, monkeypatch):
    seen = _patch_discover_recording(monkeypatch)
    _patch_cred_resolve(monkeypatch, {"type": "ssh_key", "ref": "/k", "user": "root"})
    cli_obj = _make_cli()
    assert cli_obj.process_command("/topo 10.0.0.1, 10.0.0.2 --env test --cluster c1 --yes") is True
    assert seen == ["10.0.0.1", "10.0.0.2"]


def test_topo_no_arg_prints_usage(topo_home, monkeypatch):
    cli_obj = _make_cli()
    cli_obj._prompt_text_input = lambda prompt: ""  # 回车取消
    assert cli_obj.process_command("/topo") is True
    out = _printed(cli_obj)
    assert "用法: /topo <host...> [--env E] [--user U] [--key K] [--cluster C] [--force] [--yes]" in out
    assert "已取消" in out


def test_topo_interactive_prompt_accepts_comma_hosts(topo_home, monkeypatch):
    seen = _patch_discover_recording(monkeypatch)
    _patch_cred_resolve(monkeypatch, None)
    cli_obj = _make_cli()
    answers = iter(["203.0.113.30,203.0.113.31", "dev", "ops", "/k", "ops", "/k", "y"])
    cli_obj._prompt_text_input = lambda prompt: next(answers)
    assert cli_obj.process_command("/topo") is True
    assert seen == ["203.0.113.30", "203.0.113.31"]
    assert "203.0.113.30" in _printed(cli_obj)
    assert "203.0.113.31" in _printed(cli_obj)
