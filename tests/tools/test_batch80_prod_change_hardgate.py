"""batch80（OPS-DELTA #95）：terminal prod 变更强制人工审批——执行器绕行后门收口。

背景（2026-08-25/26 定案）：runbook/生成器失败后 LLM 回退 terminal 裸命令，terminal
是黑名单模式（hardline/tirith/审批门）无动作白名单——新增风险命令（kubectl delete
namespace / set resources 等）不在黑名单即放行（smart 可自动批），等于执行器矩阵的
后门。本批在 ops_permissions 加一层：prod 环境 + 变更类动作 → 强制
require_confirmation=True（覆盖 smart 自动批，approval.py _ops_confirmation_required
机制现成），把 execute/approve 档位在 prod 变更时也升到人工确认。

行为边界：非 prod 完全不变（execute 放行、approve 走 smart）；prod + 只读动作不变
（诊断不阻）；unknown 不强制（无害命令不误伤）；矩阵已 {approve: required} 保持。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import check_ops_command_permission


def _write_matrix(home, matrix: dict) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-26T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {
            env: {act: "test" for act in cells} for env, cells in matrix.items()
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _cfg(tmp_path, env, *, enabled=True, extra="", matrix=None) -> str:
    (tmp_path / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    env: {env}\n"
        "    role: operator\n"
        f"{extra}",
        encoding="utf-8",
    )
    if matrix is not None:
        _write_matrix(tmp_path, matrix)
    return str(tmp_path)


@pytest.fixture
def perm_env(tmp_path, monkeypatch):
    def _activate(env, *, enabled=True, extra="", matrix=None):
        _cfg(tmp_path, env, enabled=enabled, extra=extra, matrix=matrix)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    from tools import ops_permissions as _op
    _op._WARNED_ENVS.clear()


# 矩阵：prod 变更动作 execute/approve 档都有（证明档位不是强制条件，env×动作才是）；
# 只读动作 approve（观察 require_confirmation=False 的决策）。
M = {
    "prod": {
        "query": "approve", "fetch_log": "approve", "verify": "approve",
        "scale": "execute", "restart": "approve",
    },
    "dev": {
        "query": "execute", "scale": "approve", "restart": "approve",
    },
    "test": {"query": "execute", "scale": "approve"},
}


def test_prod_scale_execute_level_forces_confirmation(perm_env):
    """prod + scale（矩阵 execute 档）→ 强制人工确认（矩阵档位不变，硬门在
    require_confirmation——execute/approve 档在 prod 变更时升到人工确认）。"""
    perm_env("prod", matrix=M)
    decision = check_ops_command_permission(
        "kubectl scale deployment/argocd-server --replicas=3")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["action_name"] == "scale"
    assert decision["level"] == "execute"          # 矩阵档位如实返回
    assert decision["require_confirmation"] is True
    assert "prod 变更强制人工确认" in decision["description"]


def test_prod_restart_approve_level_forces_confirmation(perm_env):
    """prod + restart（矩阵 approve 档）→ 升到强制人工确认。"""
    perm_env("prod", matrix=M)
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None
    assert decision["action_name"] == "restart"
    assert decision["level"] == "approve"
    assert decision["require_confirmation"] is True


def test_prod_query_not_forced(perm_env):
    """prod + query（只读）→ 不强制（仍走矩阵档位 approve，smart 可批）。"""
    perm_env("prod", matrix=M)
    decision = check_ops_command_permission("kubectl get pods -n argocd")
    assert decision is not None
    assert decision["action_name"] == "query"
    assert decision["level"] == "approve"
    assert decision["require_confirmation"] is False


def test_prod_fetch_log_not_forced(perm_env):
    """prod + fetch_log（只读）→ 不强制。"""
    perm_env("prod", matrix=M)
    decision = check_ops_command_permission(
        "kubectl logs -n argocd argocd-server-86678dcc97-n5cfx")
    assert decision is not None
    assert decision["action_name"] == "fetch_log"
    assert decision["require_confirmation"] is False


def test_dev_and_test_scale_not_forced(perm_env):
    """test/dev + scale → 非 prod 完全不变（approve 走 smart，不强制）。"""
    perm_env("dev", matrix=M)
    decision = check_ops_command_permission(
        "kubectl scale deployment/argocd-server --replicas=3")
    assert decision is not None
    assert decision["action_name"] == "scale"
    assert decision["level"] == "approve"
    assert decision["require_confirmation"] is False

    perm_env("test", matrix=M)
    decision = check_ops_command_permission(
        "kubectl scale deployment/argocd-server --replicas=3")
    assert decision is not None
    assert decision["action_name"] == "scale"
    assert decision["require_confirmation"] is False


def test_prod_unknown_not_forced(perm_env):
    """prod + unknown（识别不出）→ 不强制（可能是 ls/echo 等无害命令被误判）。"""
    perm_env("prod", matrix=M)
    for cmd in ("echo hello", "mv a.txt b.txt"):
        decision = check_ops_command_permission(cmd)
        assert decision is not None, cmd
        assert decision["action_name"] == "unknown", cmd
        assert decision["level"] == "approve", cmd
        assert decision["require_confirmation"] is False, cmd


def test_prod_scale_matrix_required_still_true(perm_env):
    """prod + scale 且矩阵 {approve: required} → 本来就强制（原有语义保持）。"""
    perm_env("prod", matrix={"prod": {"scale": {"approve": "required"}}})
    decision = check_ops_command_permission(
        "kubectl scale deployment/argocd-server --replicas=3")
    assert decision is not None
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True


def test_prod_missing_cell_mutating_forced(perm_env):
    """prod + 变更动作矩阵漏配（默认 approve）→ 硬门仍强制（漏配不放松）。"""
    perm_env("prod", matrix={"prod": {"query": "execute"}})
    decision = check_ops_command_permission("kubectl delete pod nginx-x")
    assert decision is not None
    assert decision["action_name"] == "decommission"
    assert decision["level"] == "approve"
    assert decision["require_confirmation"] is True


def test_prod_readonly_execute_still_passes(perm_env):
    """prod + 只读矩阵 execute → 仍直接放行（None，不弹门）。"""
    perm_env("prod", matrix={"prod": {"query": "execute"}})
    assert check_ops_command_permission("kubectl get pods -n argocd") is None


def test_e2e_smart_approve_downgraded_to_manual_for_prod_change(perm_env, monkeypatch):
    """E2E：approval 门收到 require_confirmation=True 的 prod 变更 → smart approve
    被降级为人工确认（mock _smart_approve 返回 approve 仍不自动放行）。

    矩阵 prod scale=execute（矩阵单独看直接过）——只有 hardgate 能把命令送进
    人工确认门；断言：结果不含 smart_approved（smart 未自动放行）、走 CLI 人工
    回调（user_approved=True）、弹窗文案含"prod 变更强制人工确认"。
    """
    import tools.approval as ap

    home = perm_env("prod", matrix={"prod": {"scale": "execute"}},
                    extra="approvals:\n  mode: smart\n")
    monkeypatch.setenv("VIGIL_SESSION_KEY", "batch80-e2e")
    monkeypatch.setattr(ap, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ap, "_smart_approve", lambda *a, **k: "approve")
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )

    seen = {"prompts": 0, "descriptions": []}

    def _cb(command, description, **kw):
        seen["prompts"] += 1
        seen["descriptions"].append(description)
        return "once"

    token = ap.set_hermes_interactive_context(True)
    try:
        result = ap.check_all_command_guards(
            "kubectl scale deployment/argocd-server --replicas=3", "local",
            approval_callback=_cb)
    finally:
        ap.reset_hermes_interactive_context(token)
        # 人工审批通过会登记 __user_authorized__（OPS-DELTA #25，内存态）——
        # 清理避免污染后续测试（sudo stdin guard 三态判定）。
        try:
            from tools.credential_vault import _REGISTERED
            _REGISTERED.pop("__user_authorized__", None)
        except Exception:
            pass

    assert result["approved"] is True
    assert "smart_approved" not in result, result   # smart approve 未自动放行
    assert result.get("user_approved") is True      # 走人工确认（回调）后放行
    assert seen["prompts"] == 1, seen
    assert any("prod 变更强制人工确认" in d for d in seen["descriptions"]), seen
