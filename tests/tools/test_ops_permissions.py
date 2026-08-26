"""操作矩阵权限判定（tools/ops_permissions.check_ops_command_permission）。

YAPL P5（OPS-DELTA #75）：L1-L4 命令分级退役——判定对象从"命令正则分级"换成
"动作枚举 × env 操作矩阵"（tools/action_classifier.py → tools/matrix_data.py，
与 runbook 执行路径同一 matrix.yaml）。矩阵无 deny：execute → None（直接过）；
approve → 走审批门；{approve: required} → 强制人工；unknown 动作 → 矩阵漏配
→ 默认 approve（保守，不是 deny）。无条件层（硬底线/sudo stdin/deny/ansible
guard）在 approval.py 先于本函数执行。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import (
    check_ops_command_permission,
    defined_environments,
    _map_env_tier,
)


def _write_matrix(home, matrix: dict) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-23T00:00:00+08:00",
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


T2 = {
    "prod": {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "restart": {"approve": "required"}, "reboot": {"approve": "required"},
        "deploy": {"approve": "required"}, "remove": {"approve": "required"},
        "install": {"approve": "required"}, "run_script": {"approve": "required"},
    },
    "dev": {"query": "execute", "restart": "approve", "reboot": "approve"},
    "local": {"query": "execute", "restart": "execute", "reboot": "approve"},
    "test": {"query": "execute", "restart": "execute"},
}


def test_matrix_execute_returns_none(perm_env):
    """矩阵 execute（query × prod）→ 直接过（交回原检查）。"""
    perm_env("prod", matrix=T2)
    for cmd in ("docker ps", "kubectl get pods", "systemctl status nginx"):
        assert check_ops_command_permission(cmd) is None, cmd


def test_matrix_approve_decision_shape(perm_env):
    """矩阵 approve（restart × dev）→ 审批决策（非 required）。"""
    perm_env("dev", matrix=T2)
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["action_name"] == "restart"
    assert decision["level"] == "approve"
    assert decision["env"] == "dev"
    assert decision["require_confirmation"] is False
    assert "restart" in decision["description"]


def test_matrix_required_forces_confirmation(perm_env):
    """{approve: required}（restart × prod）→ 强制人工确认。"""
    perm_env("prod", matrix=T2)
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True
    assert "强制人工确认" in decision["description"]


def test_action_missing_from_matrix_defaults_approve(perm_env):
    """矩阵漏配 action×env → 默认 approve（保守；dev 非 prod——prod 变更另走
    batch80 硬门强制人工，见 test_batch80_prod_change_hardgate）。"""
    perm_env("dev", matrix={"dev": {"query": "execute"}})
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["level"] == "approve"
    assert decision["require_confirmation"] is False


def test_unknown_action_defaults_approve(perm_env):
    """识别不出（mv/cp/echo）→ unknown → 默认 approve（不是 deny，不是 required）。"""
    perm_env("prod", matrix=T2)
    for cmd in ("mv a.txt b.txt", "cp x y", "echo hello"):
        decision = check_ops_command_permission(cmd)
        assert decision is not None, cmd
        assert decision["action_name"] == "unknown", cmd
        assert decision["level"] == "approve", cmd
        assert decision["require_confirmation"] is False, cmd
        assert "未能识别命令意图" in decision["description"], cmd


def test_chain_takes_strictest_matrix_level(perm_env):
    """链式取保守：query(execute) && restart(approve) → approve；重启 ×2 → required。"""
    perm_env("dev", matrix=T2)
    decision = check_ops_command_permission("docker ps && systemctl restart myapp")
    assert decision is not None
    assert decision["action_name"] == "restart"
    assert decision["level"] == "approve"

    perm_env("prod", matrix=T2)
    decision = check_ops_command_permission("docker ps && systemctl restart myapp")
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True
    assert "链式命令取保守" in decision["description"]


def test_ssh_controlled_channel_note(perm_env):
    """ssh → run_script 走矩阵 + 受控通道 note（§八待办收口）。"""
    perm_env("prod", matrix=T2)
    decision = check_ops_command_permission("ssh root@203.0.113.10 'df -h'")
    assert decision is not None
    assert decision["action_name"] == "run_script"
    assert decision["level"] == "required"
    assert "vssh/拓扑凭据受控通道" in decision["description"]


def test_disabled_gate_returns_none(perm_env):
    perm_env("prod", enabled=False, matrix=T2)
    assert check_ops_command_permission("systemctl restart myapp") is None


def test_empty_env_returns_none(perm_env):
    perm_env("", matrix=T2)
    assert check_ops_command_permission("systemctl restart myapp") is None


def test_non_string_or_empty_returns_none(perm_env):
    perm_env("prod", matrix=T2)
    assert check_ops_command_permission("") is None
    assert check_ops_command_permission(None) is None
    assert check_ops_command_permission("   ") is None


def test_uat_maps_to_prod_matrix_row(perm_env):
    """老 env=uat → 档位 prod → prod 矩阵行（更严不更松）。"""
    perm_env("uat", matrix=T2)
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None
    assert decision["env"] == "uat"
    assert decision["env_tier"] == "prod"
    assert decision["level"] == "required"  # prod restart = required


def test_custom_env_name_matches_matrix(perm_env):
    """自定义 env 名在矩阵里 → 精确命中（不再走档位映射）。"""
    matrix = {"sandbox": {"restart": "execute"}}
    perm_env("sandbox", matrix=matrix)
    assert check_ops_command_permission("systemctl restart myapp") is None


def test_custom_env_name_missing_matrix_maps_tier(perm_env):
    """自定义 env 名不在矩阵 → 档位映射名兜底（uat→prod）；仍无 → 默认 approve。"""
    perm_env("uat", matrix={"dev": {"restart": "approve"}})
    # uat → prod 档，但矩阵没有 prod 行 → get_level 默认 approve（保守）
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None and decision["level"] == "approve"


def test_target_env_overrides_session_env(perm_env):
    """test 会话 + 目标 prod → 按 prod 矩阵判定（跨环境硬约束）。"""
    perm_env("test", matrix=T2)
    assert check_ops_command_permission("docker ps") is None  # test query execute

    decision = check_ops_command_permission("systemctl restart myapp", target_env="prod")
    assert decision is not None
    assert decision["env"] == "prod"
    assert decision["level"] == "required"

    assert check_ops_command_permission("docker ps", target_env="prod") is None


def test_target_env_matches_session_env(perm_env):
    perm_env("prod", matrix=T2)
    decision = check_ops_command_permission("systemctl restart myapp", target_env="prod")
    assert decision is not None and decision["env"] == "prod"


def test_target_env_empty_falls_back_to_session_env(perm_env):
    perm_env("prod", matrix=T2)
    decision = check_ops_command_permission("systemctl restart myapp", target_env="")
    assert decision is not None and decision["env"] == "prod"


def test_gate_disabled_target_zero_impact(perm_env):
    perm_env("prod", enabled=False, matrix=T2)
    assert check_ops_command_permission("systemctl restart myapp", target_env="prod") is None


# ---------------------------------------------------------------------------
# 保留的 env 辅助（OPS-DELTA #75：/env 名单与 runbook env 校验继续用）
# ---------------------------------------------------------------------------

def test_legacy_env_mapping_warns_once(perm_env):
    import logging
    from tools import ops_permissions as op

    perm_env("uat", matrix=T2)
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("tools.ops_permissions")
    logger.addHandler(handler)
    try:
        check_ops_command_permission("systemctl restart myapp")
        check_ops_command_permission("systemctl restart myapp")
        check_ops_command_permission("systemctl restart myapp")
    finally:
        logger.removeHandler(handler)
    warned = [r for r in records if "不在枚举" in r]
    assert len(warned) == 1, warned


def test_map_env_tier_four_values_and_legacy(perm_env):
    perm_env("test")
    assert _map_env_tier("local") == "local"
    assert _map_env_tier("test") == "test"
    assert _map_env_tier("dev") == "dev"
    assert _map_env_tier("prod") == "prod"
    assert _map_env_tier("uat") == "prod"
    assert _map_env_tier("staging") == "dev"
    assert _map_env_tier("bare_metal_prod") == "prod"
    assert _map_env_tier("cloud") == "dev"


def test_legacy_config_env_definition_isolation_drives_tier(perm_env):
    from tools.ops_permissions import _map_env_tier

    perm_env("test", extra=(
        "  environments:\n"
        "    - {name: isolated, isolation: strict, role: isolated}\n"
        "    - {name: relaxed_zone, isolation: relaxed, role: relaxed_zone}\n"
    ))
    assert _map_env_tier("isolated") == "prod"
    assert _map_env_tier("relaxed_zone") == "dev"


def test_defined_environments_four_tiers(perm_env):
    from tools.ops_permissions import defined_environments

    perm_env("test")
    envs = defined_environments()
    assert [d["name"] for d in envs] == ["local", "test", "dev", "prod"]
    prod = next(d for d in envs if d["name"] == "prod")
    assert prod["isolation"] == "strict" and prod["role"] == "prod"


def test_defined_environments_maps_legacy_config_names(perm_env):
    from tools.ops_permissions import defined_environments

    perm_env("test", extra=(
        "  environments:\n"
        "    - {name: uat, isolation: strict, role: uat}\n"
    ))
    envs = defined_environments()
    assert [d["name"] for d in envs] == ["prod"]
    assert envs[0]["isolation"] == "strict"


def test_sudo_prefix_routes_through_same_matrix(perm_env):
    """sudo 前缀剥离 → 与 terminal 同矩阵（sudo 路径同步受益）。"""
    perm_env("prod", matrix=T2)
    decision = check_ops_command_permission("sudo systemctl restart myapp")
    assert decision is not None
    assert decision["action_name"] == "restart"
    assert decision["level"] == "required"
