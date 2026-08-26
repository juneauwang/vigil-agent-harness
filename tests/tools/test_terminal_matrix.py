"""YAPL P5 terminal 审批链（tools/approval.check_all_command_guards 操作矩阵接线）。

mock 矩阵（写 temp VIGIL_HOME/matrix.yaml）验证 terminal 直跑走操作矩阵：
- execute → 直接过（无审批卡）；
- approve → 走现有审批门（smart/manual）；
- {approve: required} → 强制人工（覆盖 mode，smart/yolo 都不能绕过）；
- unknown 动作 → 矩阵漏配 → 默认 approve（保守）+ warning；
- 无条件层（硬底线 / sudo stdin / 用户 deny / ansible inventory guard）仍最先拦
  （顺序不动，yolo/mode=off 不可绕过）；
- 一致性：同一 action × env，runbook 路径（matrix_data 直查）与 terminal 路径
  （classifier → matrix_data）档位一致。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module
from tools import matrix_data as md

SESSION = "terminal-matrix-test"


def _write_matrix(home, matrix: dict) -> None:
    path = home / "matrix.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-23T00:00:00+08:00",
        "source": "template2",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {
            env: {act: "template2" for act in cells} for env, cells in matrix.items()
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def tmatrix(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + config（env + approvals.mode）+ 定制矩阵。"""
    _session_token = approval_module.set_current_session_key(SESSION)

    def _activate(env="prod", *, mode="manual", matrix=None, extra_cfg=""):
        if matrix is None:
            matrix = {"prod": {"query": "execute", "restart": "approve",
                               "reboot": {"approve": "required"}}}
        _write_matrix(tmp_path, matrix)
        (tmp_path / "config.yaml").write_text(
            "ops:\n  permissions:\n    enabled: true\n"
            f"    env: {env}\n    role: {env}\n"
            "approvals:\n"
            f"  mode: {mode}\n"
            f"{extra_cfg}",
            encoding="utf-8",
        )
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        approval_module._YOLO_MODE_FROZEN = False
        approval_module.clear_session(SESSION)
        approval_module._permanent_approved.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    approval_module.clear_session(SESSION)
    approval_module._permanent_approved.clear()
    approval_module.reset_current_session_key(_session_token)
    from tools import ops_permissions as _op
    _op._WARNED_ENVS.clear()


def _register_gateway_auto_approve() -> None:
    """注册 gateway 通知回调：pending 条目自动按 once 批准（模拟用户在弹窗点批准）。

    走 _await_gateway_decision（非 fallback 路径），返回 user_approved=True。
    """
    def _notify(_approval_data):
        with approval_module._lock:
            entries = approval_module._gateway_queues.get(SESSION, [])
            if entries:
                entry = entries[-1]
                entry.result = "once"
                entry.event.set()

    with approval_module._lock:
        approval_module._gateway_notify_cbs[SESSION] = _notify


def _unregister_gateway() -> None:
    with approval_module._lock:
        approval_module._gateway_queues.pop(SESSION, None)
        approval_module._gateway_notify_cbs.pop(SESSION, None)


def _ask_env(monkeypatch):
    monkeypatch.setenv("VIGIL_EXEC_ASK", "1")
    monkeypatch.setenv("VIGIL_GATEWAY_SESSION", "1")


def test_execute_level_passes_directly(tmatrix):
    """矩阵 execute（query × prod）→ 直接过，无审批卡。"""
    tmatrix("prod")
    result = approval_module.check_all_command_guards("docker ps", "local")
    assert result["approved"] is True
    assert "ops_matrix" not in result
    assert result.get("user_approved") is None  # 没走人工门


def test_pipe_query_passes_directly(tmatrix):
    """词面绕过消失：docker ps | grep → query → 矩阵 execute 直接过。"""
    tmatrix("prod")
    result = approval_module.check_all_command_guards("docker ps | grep harbor", "local")
    assert result["approved"] is True
    assert "ops_matrix" not in result


def test_approve_level_rides_smart_approval(tmatrix, monkeypatch):
    """矩阵 approve 档 → smart 判 approve 可自动放行（非 required）。

    batch80（OPS-DELTA #95）起 prod 变更动作一律强制人工（见
    test_batch80_prod_change_hardgate），approve 档 smart 自动放行语义在非 prod
    （dev）验证。
    """
    tmatrix("dev", mode="smart")
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")

    result = approval_module.check_all_command_guards("docker restart harbor", "local")
    assert result["approved"] is True
    assert result.get("smart_approved") is True
    assert result.get("user_approved") is None


def test_approve_level_goes_through_human_gate(tmatrix, monkeypatch):
    """矩阵 approve → 人工门真的弹窗：gateway 回调批准后才放行（user_approved）。"""
    tmatrix("prod", mode="manual")
    _ask_env(monkeypatch)
    _register_gateway_auto_approve()
    try:
        result = approval_module.check_all_command_guards("docker restart harbor", "local")
        assert result["approved"] is True
        assert result.get("user_approved") is True
        assert result.get("smart_approved") is None  # manual 模式没走 smart
    finally:
        _unregister_gateway()


def test_required_forces_human_even_with_smart_approve(tmatrix, monkeypatch):
    """{approve: required}（restart × prod）→ smart 判 approve 也不能自动放行。

    注：systemctl reboot/poweroff 是硬底线无条件层（先于矩阵拦截），强制人工
    演示用同档位（required）的非硬底线动作 restart。
    """
    tmatrix("prod", mode="smart",
           matrix={"prod": {"query": "execute", "restart": {"approve": "required"}}})
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")
    _register_gateway_auto_approve()
    try:
        result = approval_module.check_all_command_guards("systemctl restart nginx", "local")
        assert result["approved"] is True
        # 走的是人工确认门，不是 smart 自动放行
        assert result.get("user_approved") is True
        assert result.get("smart_approved") is None
    finally:
        _unregister_gateway()


def test_required_yolo_cannot_bypass(tmatrix, monkeypatch):
    """{approve: required} → yolo 也不能绕过（强制人工）。"""
    tmatrix("prod",
           matrix={"prod": {"query": "execute", "restart": {"approve": "required"}}})
    _ask_env(monkeypatch)
    approval_module._YOLO_MODE_FROZEN = True
    _register_gateway_auto_approve()
    try:
        result = approval_module.check_all_command_guards("systemctl restart nginx", "local")
        assert result["approved"] is True
        assert result.get("user_approved") is True  # 经人工门，不是 yolo 裸放行
    finally:
        _unregister_gateway()


def test_unknown_defaults_to_approve(tmatrix, monkeypatch):
    """unknown 动作 → 矩阵漏配 → 默认 approve（走审批门，绝不 deny）。"""
    tmatrix("prod", mode="smart")
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")
    result = approval_module.check_all_command_guards("mv a.txt b.txt", "local")
    assert result["approved"] is True
    assert result.get("smart_approved") is True  # unknown → approve → smart 可判
    assert "ops_matrix" not in result  # 非 required 不强制人工


def test_unknown_description_warns_conservative(tmatrix):
    """unknown 的审批描述含保守提示（识别不出 → 默认 approve + warning）。"""
    tmatrix("prod",
           matrix={"prod": {"query": "execute", "restart": {"approve": "required"}}})
    from tools.ops_permissions import check_ops_command_permission as ck
    decision = ck("mv a.txt b.txt")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["action_name"] == "unknown"
    assert decision["require_confirmation"] is False
    assert "未能识别命令意图" in decision["description"]
    assert "OPS-DELTA 登记新规则" in decision["description"]


def test_hardline_still_blocks_before_matrix(tmatrix):
    """无条件层 1：硬底线（rm -rf /）在矩阵检查之前拦截。"""
    tmatrix("prod")
    result = approval_module.check_all_command_guards("rm -rf /", "local")
    assert result["approved"] is False
    assert "hardline" in result or "HARDLINE" in result.get("message", "") or "BLOCKED" in result.get("message", "")


def test_sudo_stdin_guard_still_blocks_before_matrix(tmatrix):
    """无条件层 2：sudo stdin 密码管道在矩阵检查之前拦截。"""
    tmatrix("prod")
    result = approval_module.check_all_command_guards(
        "echo hunter2 | sudo -S systemctl restart nginx", "local")
    assert result["approved"] is False
    assert "sudo" in result.get("message", "").lower()


def test_user_deny_rule_still_blocks_before_matrix(tmatrix):
    """无条件层 3：用户 deny 规则（approvals.deny）在矩阵检查之前拦截。"""
    tmatrix("prod", extra_cfg="  deny:\n    - \"docker restart *harbor*\"\n")
    result = approval_module.check_all_command_guards("docker restart harbor", "local")
    assert result["approved"] is False
    assert "deny" in result.get("message", "").lower() or "denied" in result.get("message", "").lower()


def test_ansible_inventory_guard_still_blocks_before_matrix(tmatrix):
    """无条件层 4：ansible inventory 契约（fail-closed）先于矩阵。"""
    tmatrix("prod")
    result = approval_module.check_all_command_guards(
        "ansible-playbook -i /nonexistent/hosts site.yml", "local")
    assert result["approved"] is False
    assert "inventory" in result.get("message", "").lower()


def test_chain_takes_strictest_matrix_level(tmatrix, monkeypatch):
    """链式取保守：query(execute) && restart(approve) → 按 restart 走审批。

    batch80 起 prod 变更动作强制人工，approve 档 smart 自动放行的链式语义在
    dev 验证；prod 验证 required 覆盖 smart（不变）。
    """
    tmatrix("dev", mode="smart")
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")
    result = approval_module.check_all_command_guards(
        "docker ps && docker restart harbor", "local")
    assert result["approved"] is True
    assert result.get("smart_approved") is True  # approve 档，非 required

    # restart(approve) && systemctl restart(required) → 取 required → 强制人工
    tmatrix("prod", mode="smart",
           matrix={"prod": {"query": "execute", "restart": {"approve": "required"}}})
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")
    _register_gateway_auto_approve()
    try:
        result = approval_module.check_all_command_guards(
            "docker restart harbor && systemctl restart nginx", "local")
        assert result["approved"] is True
        assert result.get("user_approved") is True
        assert result.get("smart_approved") is None  # required 覆盖 smart
    finally:
        _unregister_gateway()


def test_terminal_runbook_consistency(tmatrix):
    """一致性：同一 action × env，terminal 路径（classifier → 矩阵）与 runbook
    路径（matrix_data 直查）档位一致。"""
    home = tmatrix("prod")
    matrix = md.load_matrix_or_empty(home)
    from tools.ops_permissions import check_ops_command_permission as ck
    from tools.action_classifier import classify_command

    pairs = [
        ("docker ps", "query", "prod"),
        ("docker restart harbor", "restart", "prod"),
        ("systemctl reboot", "reboot", "prod"),
        ("kubectl get pods", "query", "prod"),
    ]
    for cmd, action, env in pairs:
        assert classify_command(cmd)["action"] == action, cmd
        # runbook 路径：matrix_data 直查（P4 执行器同一读路径）
        runbook_level = md.get_level(matrix, env, action)["level"]
        # terminal 路径：classifier → matrix_data（check_ops_command_permission 内部）
        decision = ck(cmd, target_env=env)
        terminal_level = (
            "execute" if decision is None else decision["level"]
        )
        assert terminal_level == runbook_level, (cmd, terminal_level, runbook_level)
