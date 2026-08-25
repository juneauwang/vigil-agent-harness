"""batch76（OPS-DELTA #91）：runbook_create 解除存在性门控测试。

背景（dogfood 实证）：runbook_create/load/checkpoint 三个工具全挂
check_fn=check_runbook_requirements（runbooks/ 有 yaml 才可用）→ 目录空时
runbook_create 不出现在 LLM 工具列表 → LLM 想创建 runbook 没工具可用 →
绕行直接写 YAML 文件，绕过资产审批门和三层校验器。

修法：runbook_create 去掉 check_fn（始终可用——目录空正是它该工作的时候）；
runbook_load/runbook_checkpoint 保留存在性门控（读/执行依赖存量数据，
空目录工具不出现在列表，合理）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tools.registry import invalidate_check_fn_cache, registry
from tools.runbook_tools import runbook_load
from toolsets import resolve_toolset

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_RUNBOOKS = PROJECT_ROOT / "hermes_cli" / "ops_samples" / "runbooks"


@pytest.fixture
def rb_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    invalidate_check_fn_cache()
    try:
        yield home
    finally:
        invalidate_check_fn_cache()


def _runbook_schema_names():
    return {
        s["function"]["name"]
        for s in registry.get_definitions(set(resolve_toolset("runbook")), quiet=True)
        if "function" in s
    }


def test_create_available_with_empty_runbooks(rb_home):
    """空 runbooks/ → runbook_create 工具出现在 LLM 工具列表。"""
    assert not any(rb_home.glob("runbooks/*.yaml"))
    names = _runbook_schema_names()
    assert "runbook_create" in names


def test_load_gated_off_with_empty_runbooks(rb_home):
    """空 runbooks/ → runbook_load 不出现在工具列表（存在性门控保留）。"""
    names = _runbook_schema_names()
    assert "runbook_load" not in names


def test_all_three_tools_available_with_data(rb_home):
    """非空 runbooks/ → runbook_create/load/checkpoint 都在工具列表。"""
    for src in sorted(SAMPLE_RUNBOOKS.glob("*.yaml")):
        shutil.copy2(src, rb_home / "runbooks" / src.name)
    invalidate_check_fn_cache()
    names = _runbook_schema_names()
    assert {"runbook_create", "runbook_load", "runbook_checkpoint"} <= names


def test_create_available_when_load_reports_no_data(rb_home):
    """空目录下 runbook_load 调用报无数据，runbook_create 仍可注册。"""
    result = runbook_load(home=rb_home)
    assert "runbooks" in result  # count=0 列表或数据不存在错误，均无 runbook 内容
    assert '"count": 0' in result or "数据不存在" in result
    entry = registry.get_entry("runbook_create")
    assert entry is not None and entry.check_fn is None
