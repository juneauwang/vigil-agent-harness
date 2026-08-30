"""Tests for the /env slash command (OPS-DELTA #11 — session operation env).

covers: command registration, no-arg listing, switch persistence (config +
_active_env + banner badge), unknown-env error, and the permission matrix
following the switch for a custom env name.
"""

from unittest.mock import MagicMock

import pytest
import yaml

from cli import HermesCLI
from hermes_cli.commands import resolve_command
import hermes_cli.banner as banner
import hermes_cli.config as hc
from tools.ops_permissions import _active_env, check_ops_command_permission

CONFIG_TPL = """\
ops:
  environments:
    - name: test
      isolation: relaxed
      role: test
    - name: bare_metal_prod
      isolation: strict
      role: prod
    - name: local
      isolation: relaxed
      role: test
  permissions:
    enabled: true
    env: {env}
    role: {env}
"""


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
    # 非交互路径会走 show_banner()——stub 掉避免控制台噪音
    cli_obj.show_banner = lambda: None
    return cli_obj


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        CONFIG_TPL.format(env="test"), encoding="utf-8"
    )
    # 初始化矩阵：P5 矩阵语义（unknown → 默认 approve）在矩阵存在时对任意 env
    # 全量生效；矩阵未初始化（OPS-DELTA #76 惰性）只对 prod 档门控、test 档
    # 交回原检查——与本测试"矩阵跟随 /env 切换"的意图不符，故 fixture 显式
    # 初始化（对齐真实运维环境：配置自定义 env 的部署已跑过 vigil matrix init）。
    (tmp_path / "matrix.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "updated_at": "2026-08-23T00:00:00+08:00",
            "source": "test",
            "base_template": "template2",
            "matrix": {"prod": {"restart": {"approve": "required"}}},
            "sources": {"prod": {"restart": "template2"}},
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    banner._banner_state_cache = None
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    banner._banner_state_cache = None


def _printed(cli_obj) -> str:
    return "\n".join(str(c.args[0]) for c in cli_obj.console.print.call_args_list)


def test_env_command_registered_in_cli_registry():
    cmd = resolve_command("env")
    assert cmd is not None
    assert cmd.name == "env"
    assert cmd.cli_only is True
    assert cmd.category == "Session"


def test_env_no_args_lists_current_and_available(env_home):
    cli_obj = _make_cli()
    assert cli_obj.process_command("/env") is True
    out = _printed(cli_obj)
    assert "当前操作环境" in out
    assert "test" in out
    assert "bare_metal_prod" in out
    assert "local" in out


def test_env_switch_updates_config_active_env_and_banner(env_home):
    cli_obj = _make_cli()
    assert cli_obj.process_command("/env bare_metal_prod") is True

    hc._LOAD_CONFIG_CACHE.clear()
    perms = hc.load_config_readonly()["ops"]["permissions"]
    assert perms["env"] == "bare_metal_prod"
    # role 跟随所选环境定义的 role（不是环境名本身）
    assert perms["role"] == "prod"
    assert _active_env() == "bare_metal_prod"
    # banner ENV badge 数据源随切换更新
    assert banner._load_banner_state()["env"] == "bare_metal_prod"
    # 操作矩阵按自定义 env 名判定（YAPL P5：unknown 动作 → 默认 approve 保守；
    # batch83 起 rm 归 remove 高危变更 → 目标解析，unknown 语义用 git push 验证）
    decision = check_ops_command_permission("git push origin main")
    assert decision is not None and decision["action"] == "approve"
    assert decision["env"] == "bare_metal_prod"


def test_env_switch_back_to_builtin_name(env_home):
    cli_obj = _make_cli()
    assert cli_obj.process_command("/env bare_metal_prod") is True
    assert cli_obj.process_command("/env test") is True
    assert _active_env() == "test"
    assert banner._load_banner_state()["env"] == "test"


def test_env_unknown_env_errors_and_keeps_current(env_home):
    cli_obj = _make_cli()
    assert cli_obj.process_command("/env nope") is True
    out = _printed(cli_obj)
    assert "未定义环境" in out
    assert "bare_metal_prod" in out  # 报错列出可用项
    assert _active_env() == "test"
    # test 档：unknown 动作 → 默认 approve（矩阵语义，非 deny；batch83 起
    # rm → remove 高危变更走目标解析，unknown 语义用 git push 验证）
    decision = check_ops_command_permission("git push origin main")
    assert decision is not None and decision["action"] == "approve"


def test_env_switch_persists_across_cache_clear(env_home):
    """切换写 config（持久），清缓存后 _active_env 仍为新 env（跨会话保留）。"""
    cli_obj = _make_cli()
    assert cli_obj.process_command("/env local") is True
    hc._LOAD_CONFIG_CACHE.clear()
    assert _active_env() == "local"
