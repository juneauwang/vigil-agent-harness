"""task103 任务 4：矩阵新增 `unresolved` 行 + 堵 get_level 隐式放水口。

反证：基线 8ba4b5ce 四模板无 `unresolved` 行（走 get_level 默认 approve）；
env 名不在矩阵 → get_level 默认 approve。断言 required/存在均失败。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from hermes_cli.subcommands import matrix as matrix_cli
from tools import matrix_data as md

READONLY = ("query", "fetch_log", "verify")


@pytest.fixture
def mhome(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.mark.parametrize("template", ["template1", "template2", "template3"])
def test_templates_have_unresolved_row_all_required(mhome, template):
    data = md.init_matrix(template, home=mhome, force=True)
    row = data["matrix"]["unresolved"]
    for action in READONLY + ("restart", "install", "remove", "apply_config"):
        assert row[action] == md.LEVEL_REQUIRED, (template, action, row[action])
    # runbook 与其余模板同款不进 setup 四模板
    assert "runbook" not in row


def test_template4_has_unresolved_row_all_required(mhome):
    data = md.init_matrix(
        "template4", home=mhome, force=True,
        selections={"execute": ["query"], "approve": ["restart"]},
    )
    row = data["matrix"]["unresolved"]
    assert row["query"] == md.LEVEL_REQUIRED
    assert row["restart"] == md.LEVEL_REQUIRED
    assert "unresolved" in data["sources"]


def test_set_level_unresolved_roundtrip(mhome):
    md.init_matrix("template2", home=mhome, force=True)
    data = md.load_matrix(mhome)
    summary = md.set_level(data, "unresolved", "query", md.LEVEL_EXECUTE)
    assert summary["changed"] is True
    md.write_matrix(data, home=mhome)
    reloaded = md.load_matrix(mhome)  # 校验器不报错
    assert md.get_level(reloaded, "unresolved", "query")["level"] == "execute"
    assert md.get_level(reloaded, "unresolved", "restart")["level"] == "required"


def test_get_level_unknown_env_returns_required(mhome):
    md.init_matrix("template2", home=mhome, force=True)
    data = md.load_matrix(mhome)
    level = md.get_level(data, "ghost_env", "restart")
    assert level["level"] == md.LEVEL_REQUIRED
    assert level["configured"] is False


def test_get_level_missing_action_still_approve(mhome):
    md.init_matrix("template2", home=mhome, force=True)
    data = md.load_matrix(mhome)
    level = md.get_level(data, "prod", "runbook")
    assert level["level"] == md.LEVEL_APPROVE
    assert level["configured"] is False


def test_cli_init_and_set_unresolved(mhome):
    assert matrix_cli.main(["init", "--template", "2"]) == 0
    assert matrix_cli.main(["set", "restart", "unresolved", "execute"]) == 0
    data = md.load_matrix(mhome)
    assert md.get_level(data, "unresolved", "restart")["level"] == "execute"
