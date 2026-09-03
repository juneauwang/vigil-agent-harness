"""batch86 信号采集端到端 —— 审批门用户决策 → approval_memory（OPS-DELTA #102）。

覆盖验收（红线 1 + 交互 opt-in）：e 首次弹审批 → 批准 → count=1 未沉淀，第 3
次批准 → active；f 批准 2 次后拒绝 1 次 → banned，之后成功 N 次也不沉淀；
g/q 审批交互选『以后不用问』（learn）→ user_opt_in 立即沉淀（1 次即可）；
h 拒绝事件确实被捕获（不是只看执行成功）。smart/yolo/cron 自动放行不产生
用户信号 → 不入计数（红线 1：不能只看执行成功）。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools import approval_memory as am, terminal_tool
from tools.approval import request_ops_approval

_APPROVE_DECISION = {
    "action": "approve",
    "action_name": "unknown",
    "env": "prod",
    "require_confirmation": False,
    "description": "矩阵 unknown × prod 需审批",
}


@pytest.fixture(autouse=True)
def _gate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
    hc._LOAD_CONFIG_CACHE.clear()
    am._read_cache.clear()
    _holder = {"choice": "once"}

    def _cb(command, description, **kwargs):
        return _holder["choice"]

    terminal_tool.set_approval_callback(_cb)
    yield _holder
    terminal_tool.set_approval_callback(None)
    monkeypatch.delenv("VIGIL_INTERACTIVE", raising=False)
    hc._LOAD_CONFIG_CACHE.clear()
    am._read_cache.clear()


def _approve_once(command, n=1):
    for _ in range(n):
        result = request_ops_approval(command, dict(_APPROVE_DECISION))
        assert result.get("approved") is True, result
    return result


# --- e：批准计数沉淀（每次 once = 一次真实用户决策） ---

def test_approval_counts_then_activates(_gate_env):
    _approve_once("sudo lsblk", n=2)
    entry = am.get_entry("lsblk")
    assert entry is not None and entry["success_count"] == 2
    assert entry["status"] == "pending" and not am.is_active("lsblk")
    _approve_once("sudo lsblk", n=1)
    assert am.is_active("lsblk")
    assert am.get_entry("lsblk")["success_count"] == 3


# --- f：批准后拒绝 → banned，成功 N 次不复活 ---

def test_deny_after_approvals_bans(_gate_env):
    _gate_env["choice"] = "once"
    _approve_once("sudo newtool", n=2)
    _gate_env["choice"] = "deny"
    result = request_ops_approval("sudo newtool", dict(_APPROVE_DECISION))
    assert result.get("approved") is False
    assert am.get_entry("newtool")["status"] == "banned"
    _gate_env["choice"] = "once"
    for _ in range(5):
        request_ops_approval("sudo newtool", dict(_APPROVE_DECISION))
    assert am.get_entry("newtool")["status"] == "banned"
    assert not am.is_active("newtool")


# --- g/q：learn（以后不用问）→ 立即沉淀 ---

def test_learn_choice_immediate_opt_in(_gate_env):
    _gate_env["choice"] = "learn"
    result = _approve_once("sudo smartctl -x")
    entry = am.get_entry("smartctl")
    assert entry["status"] == "active"
    assert entry["user_opt_in"] is True
    assert entry["success_count"] == 1
    assert am.command_template_approved("sudo smartctl -x") is True


# --- h：拒绝事件被捕获；被拒命令执行成功也不沉淀 ---

def test_denial_captured_not_just_execution_success(_gate_env):
    # 先批准一次（count=1）→ 拒绝 → banned（同 f）
    _gate_env["choice"] = "once"
    _approve_once("sudo mydiag -x")
    _gate_env["choice"] = "deny"
    request_ops_approval("sudo mydiag -x", dict(_APPROVE_DECISION))
    assert am.get_entry("mydiag")["status"] == "banned"
    # 用户之后又批准成功 N 次（执行成功路径）→ banned 不解除、不沉淀
    _gate_env["choice"] = "once"
    for _ in range(4):
        request_ops_approval("sudo mydiag -x", dict(_APPROVE_DECISION))
    assert am.get_entry("mydiag")["status"] == "banned"


# --- 合成目标（plugin/asset 门）不产生命令信号 ---

def test_synthetic_targets_produce_no_signal(_gate_env):
    from tools.approval import request_tool_approval
    _gate_env["choice"] = "deny"
    request_tool_approval("write_file", "plugin 需要审批", rule_key="test-rule")
    assert not am.memory_path().is_file()  # 无命令条目落盘
