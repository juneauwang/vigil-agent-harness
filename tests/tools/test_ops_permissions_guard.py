"""Ops permission matrix integrated in the real command guard (tools/approval.py)."""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module

SESSION = "ops-guard-test"


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    approval_module.set_current_session_key(SESSION)

    def _activate(env="prod", *, enabled=True):
        (tmp_path / "config.yaml").write_text(
            "ops:\n"
            "  permissions:\n"
            f"    enabled: {str(enabled).lower()}\n"
            f"    env: {env}\n"
            f"    role: {env}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        approval_module._YOLO_MODE_FROZEN = False
        approval_module.clear_session(SESSION)
        approval_module._permanent_approved.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    approval_module.clear_session(SESSION)


def test_matrix_deny_is_hard_block_even_with_yolo(guard_env, monkeypatch):
    guard_env("prod")
    approval_module._YOLO_MODE_FROZEN = True  # 会话级 bypass 无法绕过矩阵拒绝
    result = approval_module.check_all_command_guards("kubectl delete namespace prod", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action"] == "deny"
    assert "Do NOT retry" in result["message"]


def test_matrix_approve_rides_smart_approval(guard_env, monkeypatch):
    guard_env("prod")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")

    result = approval_module.check_all_command_guards("systemctl restart myapp", "local")
    assert result["approved"] is True
    assert result["smart_approved"] is True


def test_matrix_approve_fails_closed_without_human(guard_env, monkeypatch):
    guard_env("prod")
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    result = approval_module.check_all_command_guards("systemctl restart myapp", "local")
    assert result["approved"] is False
    assert "no interactive user or gateway" in result["message"]


def test_gate_disabled_leaves_existing_flow_unchanged(guard_env, monkeypatch):
    guard_env("prod", enabled=False)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "manual"})

    # pip install 不在 DANGEROUS_PATTERNS；gate 关闭 → 直接通过
    result = approval_module.check_all_command_guards("pip install requests", "local")
    assert result["approved"] is True


def test_matrix_deny_in_uat_and_test(guard_env, monkeypatch):
    guard_env("uat")
    result = approval_module.check_all_command_guards("kubectl delete namespace prod", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action"] == "deny"

    guard_env("test")
    # test 环境 L4 = 直接执行（自用环境）
    result = approval_module.check_all_command_guards("kubectl delete namespace prod", "local")
    assert result["approved"] is True
