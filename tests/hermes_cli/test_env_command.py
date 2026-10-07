"""Tests for the retired /env slash command (OPS-DELTA #103 / yapl-design §11.8).

#103 定案：env 维度一律取自**操作目标实体**（拓扑表），会话声明的
``ops.permissions.env`` 不参与任何裁决。既然它不再决定任何事，载体就必须退场
——留一个"改了没用"的旋钮正是 OPS-DELTA #11 的翻车模式（同一个坑，换个形式）。

本文件是**退场守卫**：断言三条载体都不复存在（命令 / banner ENV 行 / prompt
"[env]" 前缀），并保留 #103 的核心语义守卫（config env 改了也不影响裁决）。

反证（起点 3ff1ab38）：/env 仍在 COMMAND_REGISTRY、仍可 resolve、banner 仍渲染
ENV 行、prompt 仍带 "[env]" 前缀 → 本文件三条载体断言全失败。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

import hermes_cli.banner as banner
import hermes_cli.config as hc
from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
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
  permissions:
    enabled: true
    env: {env}
    role: {env}
"""


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CONFIG_TPL.format(env="test"), encoding="utf-8")
    (tmp_path / "matrix.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "updated_at": "2026-10-07T00:00:00+08:00",
            "source": "test",
            "base_template": "template2",
            "matrix": {"prod": {"restart": {"approve": "required"}}},
            "sources": {"prod": {"restart": "template2"}},
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # env 一律取自目标实体（拓扑表）——本机 workstation(local) 供无 token 命令解析。
    (tmp_path / "topology.yaml").write_text(
        "version: 4\n"
        "environments:\n"
        "  - {name: local, isolation: relaxed, role: local}\n"
        "hosts:\n"
        "  - {name: workstation, type: host, env: local, endpoint: \"192.168.1.50\"}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tools.target_resolve._local_host_identities", lambda: ["workstation"])
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    banner._banner_state_cache = None
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    banner._banner_state_cache = None


# ── 载体①：slash 命令 ───────────────────────────────────────────────────────

def test_env_command_retired_from_registry():
    """/env 已从 COMMAND_REGISTRY 摘除，resolve_command 不再认得它。"""
    assert "env" not in {c.name for c in COMMAND_REGISTRY}
    assert resolve_command("env") is None
    assert resolve_command("/env") is None


# ── 载体②③：banner ENV 行 / prompt "[env]" 前缀（源码级守卫）───────────────

def test_banner_env_row_retired():
    """banner 不再渲染 ENV 行（#103 前是 `ENV         [test]`）。"""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "hermes_cli" / "banner.py").read_text(encoding="utf-8")
    assert 'ENV[/]' not in src


def test_prompt_env_prefix_retired():
    """TUI prompt 不再带 "[env] " 前缀（#103 前是 `[test] >`）。"""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "cli.py").read_text(encoding="utf-8")
    assert "class:ops-env-" not in src


# ── #103 核心语义守卫：config env 不再驱动裁决 ──────────────────────────────

def test_config_env_change_does_not_drive_adjudication(env_home):
    """改 config ops.permissions.env（字段仍在，读取兼容）不影响裁决——env 取自目标实体。"""
    (env_home / "config.yaml").write_text(
        CONFIG_TPL.format(env="bare_metal_prod"), encoding="utf-8")
    hc._LOAD_CONFIG_CACHE.clear()
    assert _active_env() == "bare_metal_prod"  # 兼容读取仍在
    decision = check_ops_command_permission("git push origin main")
    assert decision is not None and decision["action"] == "approve"
    assert decision["env"] == "local"  # 裁决取目标实体（本机 workstation=local）
