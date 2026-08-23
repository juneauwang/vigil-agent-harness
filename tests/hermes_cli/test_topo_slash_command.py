"""批次十八 A——/topo 斜杠命令验收测试。

覆盖：注册表（/topo in COMMANDS + resolve_command）、有 host 直接调发现引擎 +
凭据从拓扑 credential 解析、无参交互收集（mock 输入流）、结果展示含
needs_review 引导、落盘联动（write_discovery merge 参数传递 + y/N 确认默认 N +
--yes 跳过 + --force → merge=False）。
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from cli import HermesCLI
from hermes_cli.commands import COMMANDS, resolve_command
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
        "version": 3,
        "source": "discovered",
        "last_verified": "2026-08-14",
        "needs_review": True,
        "host": {
            "name": host, "env": env, "cluster": "default", "endpoint": host,
            "runtime": "docker", "source": "discovered", "needs_review": True,
            "last_verified": "2026-08-14",
            "services_index": f"hosts/{host}.yaml",
        },
        "services": [
            {"name": "app", "type": "service", "env": env, "cluster": "default",
             "endpoint": f"{host}:8080", "source": "discovered",
             "last_verified": "2026-08-14", "needs_review": True,
             "detail": f"entities/prod__{host}__app.yaml", "attrs": {}},
        ],
        "details": {
            "app": {"name": "app", "type": "service", "env": env,
                    "cluster": "default", "detail": f"entities/prod__{host}__app.yaml",
                    "attrs": {}, "source": "discovered", "last_verified": "2026-08-14",
                    "needs_review": True},
        },
        "pending_review": [],
        "probes": {"docker": "ok", "ss": "ok"},
    }


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _patch_discover(monkeypatch, discovery=None):
    import tools.topo_discovery as td
    calls = {}

    def fake_discover(host, env, creds, *, cluster=""):
        calls["host"] = host
        calls["env"] = env
        calls["creds"] = creds
        calls["cluster"] = cluster
        return discovery(host, env) if callable(discovery) else (_discovery(host, env))

    monkeypatch.setattr(td, "discover_host", fake_discover)
    return calls


def _patch_cred_resolve(monkeypatch, cred):
    import hermes_cli.subcommands.vssh as vssh
    monkeypatch.setattr(vssh, "_resolve_topology_credential", lambda host: cred)


def test_topo_command_registered_in_cli_registry():
    assert "/topo" in COMMANDS
    cmd = resolve_command("topo")
    assert cmd is not None
    assert cmd.name == "topo"
    assert cmd.cli_only is True
    assert cmd.category == "Session"
    assert "--env" in (cmd.args_hint or "")


def test_topo_with_host_calls_engine_and_uses_topology_credential(topo_home, monkeypatch):
    calls = _patch_discover(monkeypatch)
    _patch_cred_resolve(
        monkeypatch, {"type": "ssh_key", "ref": "/keys/id_ed25519", "user": "root"}
    )
    cli_obj = _make_cli()
    assert cli_obj.process_command("/topo 8.140.60.44 --env prod --yes") is True
    assert calls["host"] == "8.140.60.44"
    assert calls["env"] == "prod"
    assert calls["cluster"] == ""
    assert calls["creds"]["user"] == "root"
    assert calls["creds"]["key_path"] == "/keys/id_ed25519"
    out = _printed(cli_obj)
    assert "8.140.60.44" in out
    assert "app" in out
    assert "needs_review" in out
    # 三步 review 引导（topo_query 查看 / topo_update 确认 / 权威拓扑）。
    assert "topo_query" in out and "topo_update" in out
    assert "needs_review=false" in out
    # 落盘联动：合并语义落盘 + 计数打印。
    assert "追加 1 个 / 保留 0 个 / 系统服务 0 个未入表" in out


def test_topo_no_host_interactive_collection(topo_home, monkeypatch):
    calls = _patch_discover(monkeypatch)
    _patch_cred_resolve(monkeypatch, None)  # 拓扑无凭据 → 交互问 user/key
    cli_obj = _make_cli()
    answers = iter(["8.140.60.44", "prod", "ops", "/keys/id_ed25519", "y"])
    cli_obj._prompt_text_input = lambda prompt: next(answers)

    assert cli_obj.process_command("/topo") is True
    assert calls["host"] == "8.140.60.44"
    assert calls["env"] == "prod"
    assert calls["creds"]["user"] == "ops"
    assert calls["creds"]["key_path"] == "/keys/id_ed25519"
    # 交互收集成功落盘（合并语义默认 merge=True；v0.4 第二层目录 services/，
    # batch52 拓扑 schema v0.4 分层后 hosts/ 不再使用）。
    index = topo_home / "services" / "8.140.60.44.yaml"
    assert index.is_file()
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    assert {s["name"] for s in data["services"]} == {"app"}


def test_topo_interactive_cancel_on_empty_host(topo_home, monkeypatch):
    cli_obj = _make_cli()
    cli_obj._prompt_text_input = lambda prompt: ""  # 回车取消
    assert cli_obj.process_command("/topo") is True
    out = _printed(cli_obj)
    assert "已取消" in out
    assert not (topo_home / "topology.yaml").exists()


def test_topo_confirm_default_no_write(topo_home, monkeypatch):
    _patch_discover(monkeypatch)
    _patch_cred_resolve(monkeypatch, {"type": "ssh_key", "ref": "/k", "user": "root"})
    cli_obj = _make_cli()
    cli_obj._prompt_text_input = lambda prompt: ""  # 确认默认 N

    assert cli_obj.process_command("/topo 8.140.60.44 --env prod") is True
    out = _printed(cli_obj)
    assert "将写入" in out
    assert "已取消，未写入任何文件" in out
    assert not (topo_home / "topology.yaml").exists()
    assert not (topo_home / "hosts").exists()


def test_topo_write_passes_merge_and_force(topo_home, monkeypatch):
    import tools.topo_discovery as td

    _patch_discover(monkeypatch)
    _patch_cred_resolve(monkeypatch, {"type": "ssh_key", "ref": "/k", "user": "root"})
    write_calls = {}

    def fake_write(home_, discovery, force=False, merge=True):
        write_calls["force"] = force
        write_calls["merge"] = merge
        return {"written": ["hosts/8.140.60.44.yaml"], "appended": 1, "kept": 0}

    monkeypatch.setattr(td, "write_discovery", fake_write)

    cli_obj = _make_cli()
    cli_obj._prompt_text_input = lambda prompt: "y"
    assert cli_obj.process_command("/topo 8.140.60.44 --env prod") is True
    assert write_calls["merge"] is True
    assert write_calls["force"] is False

    write_calls.clear()
    assert cli_obj.process_command("/topo 8.140.60.44 --env prod --force --yes") is True
    assert write_calls["force"] is True
    assert write_calls["merge"] is False


def test_topo_unknown_flag_reports_error(topo_home, monkeypatch):
    cli_obj = _make_cli()
    assert cli_obj.process_command("/topo 8.140.60.44 --env prod --nope") is True
    out = _printed(cli_obj)
    assert "未知参数 --nope" in out


def test_topo_host_without_env_requires_flag(topo_home, monkeypatch):
    cli_obj = _make_cli()
    assert cli_obj.process_command("/topo 8.140.60.44") is True
    out = _printed(cli_obj)
    assert "--env" in out
