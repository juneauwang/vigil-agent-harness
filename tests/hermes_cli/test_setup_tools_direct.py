"""批次十二 D1：setup 工具选择直给——不再展示消费级工具勾选清单。

运维用户不该被问"要配哪些工具"：quick setup 的 Tool API Keys checklist 整段
移除；消费级工具（category=tool 非运维项）标 advanced=true（setup 不再问、
dashboard 折叠），`vigil tools` 仍可手动开（_DEFAULT_OFF_TOOLSETS 语义不变）。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from hermes_cli import setup as setup_mod
from hermes_cli.config_defaults import OPTIONAL_ENV_VARS


@pytest.fixture
def quick_env(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir()
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def test_quick_setup_never_asks_which_tools(quick_env, monkeypatch, capsys):
    """quick setup 流程不再出现工具勾选清单（无 'Which tools' / 'Tool API Keys'）。"""
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.setup import _run_quick_setup

    checklist_questions = []

    def fake_checklist(question, choices, pre_selected=None):
        checklist_questions.append(question)
        return []

    monkeypatch.setattr(setup_mod, "prompt_checklist", fake_checklist)
    monkeypatch.setattr(setup_mod, "prompt", lambda *a, **kw: "")
    # batch83-fix（OPS-DELTA #99）：quick setup 新增权限矩阵步骤（矩阵缺失 →
    # fail-closed，quick 也保证 matrix.yaml 存在）——模板选择默认模板 2。
    monkeypatch.setattr(setup_mod, "prompt_choice", lambda *a, **kw: 1)

    cfg = dict(DEFAULT_CONFIG)
    _run_quick_setup(cfg, quick_env)

    out = capsys.readouterr().out
    assert "Which tools" not in out
    assert "Tool API Keys" not in out
    assert not any("tools" in q.lower() for q in checklist_questions)


def test_consumer_tool_env_vars_are_advanced(quick_env):
    """消费级工具 env 条目标 advanced=true（setup 不再问）；off-toolsets 语义不变。"""
    tool_entries = [v for v in OPTIONAL_ENV_VARS.values() if v.get("category") == "tool"]
    assert tool_entries
    assert all(v.get("advanced") is True for v in tool_entries)

    from hermes_cli.tools_config import _DEFAULT_OFF_TOOLSETS
    # `vigil tools` 仍可手动开启被裁工具（回归：D1 只收 setup 面，不删能力）。
    assert {"browser", "image_gen", "video_gen"} <= set(_DEFAULT_OFF_TOOLSETS)
