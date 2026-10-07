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


def _cfg(tmp_path, env, *, enabled=True, extra="", matrix=None,
         topo_env=None) -> str:
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
    # #103：env 一律取自目标实体（拓扑表）。本机（无 token 命令）登记为
    # workstation（env 跟随 topo_env），另铺单集群 + prod 主机供 token 命令解析。
    te = topo_env if topo_env is not None else (env or "test")
    (tmp_path / "topology.yaml").write_text(
        "version: 4\n"
        "environments:\n"
        f"  - {{name: {te}, isolation: relaxed, role: {te}}}\n"
        f"  - {{name: prod, isolation: strict, role: prod}}\n"
        "clusters:\n"
        f"  - {{name: k3s-{te}, env: {te}, type: k3s, endpoint: \"https://203.0.113.15:6443\"}}\n"
        "hosts:\n"
        f"  - {{name: workstation, type: host, env: {te}, endpoint: \"192.168.1.50\"}}\n"
        "  - {name: node-prod, type: host, env: prod, endpoint: \"203.0.113.11\"}\n",
        encoding="utf-8",
    )
    return str(tmp_path)


@pytest.fixture
def perm_env(tmp_path, monkeypatch):
    def _activate(env, *, enabled=True, extra="", matrix=None, topo_env=None):
        _cfg(tmp_path, env, enabled=enabled, extra=extra, matrix=matrix,
             topo_env=topo_env)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        monkeypatch.setattr(
            "tools.target_resolve._local_host_identities", lambda: ["workstation"])
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
    """矩阵漏配 action×env → 默认 approve（保守；#103 起硬门退役，不再升级）。"""
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
    """ssh → run_script 走矩阵 + 受控通道 note（§八待办收口）。

    batch83（OPS-DELTA #98）起 ssh 受控通道强制目标解析：拓扑表登记目标主机
    才放行到矩阵（未登记 → deny）。本例铺 node1（203.0.113.10, prod）。
    """
    home = perm_env("prod", matrix=T2)
    (home / "topology.yaml").write_text(
        "version: 1\n"
        "environments:\n"
        "  - {name: prod, isolation: strict, role: prod, core_entities: [node1]}\n"
        "core_entities:\n"
        "  - {name: node1, type: host, env: prod, endpoint: \"203.0.113.10\"}\n",
        encoding="utf-8",
    )
    decision = check_ops_command_permission("ssh root@203.0.113.10 'df -h'")
    assert decision is not None
    assert decision["action_name"] == "run_script"
    assert decision["level"] == "required"
    assert decision["env"] == "prod"
    assert "vssh/拓扑凭据受控通道" in decision["description"]


def test_disabled_gate_returns_none(perm_env):
    perm_env("prod", enabled=False, matrix=T2)
    assert check_ops_command_permission("systemctl restart myapp") is None


def test_empty_config_env_still_adjudicates_by_target_entity(perm_env):
    """#103：config env 空不再关闭判定——env 取自目标实体（本机 workstation=test）。"""
    perm_env("", topo_env="test", matrix=T2)
    # test 行 restart=execute → 无审批；证明判定按实体 env 进行而非"空 env 直接放行"
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
    """自定义 env 名不在矩阵 → 档位映射名兜底（uat→prod）；仍无 → required（#103）。"""
    perm_env("uat", matrix={"dev": {"restart": "approve"}})
    # uat → prod 档，但矩阵没有 prod 行 → env 名不在矩阵 → required（堵默认 approve
    # 放水口，#103 任务 4：不许自动放行、可人工放行）
    decision = check_ops_command_permission("systemctl restart myapp")
    assert decision is not None and decision["level"] == "required"


def test_config_env_does_not_drive_adjudication(perm_env):
    """#103：config env 不再参与裁决——env 取自目标实体。

    config env=prod，但本机实体 env=test → 无 token 命令落 test 行（restart
    execute），证明配置里的 env 没有把判定拉到 prod required。
    """
    perm_env("prod", topo_env="test", matrix=T2)
    assert check_ops_command_permission("systemctl restart myapp") is None
    # token 命令仍按目标实体：kubectl → 单集群 k3s-test → test query execute
    assert check_ops_command_permission("kubectl get pods") is None


def test_target_entity_env_drives_cross_session(perm_env):
    """test 会话 + ssh 打 prod 实体（node-prod）→ 按 prod 矩阵 required。"""
    perm_env("test", matrix=T2)
    assert check_ops_command_permission("docker ps") is None  # 本机 test query execute

    decision = check_ops_command_permission("ssh root@203.0.113.11 'df -h'")
    assert decision is not None
    assert decision["env"] == "prod"
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True


def test_target_env_param_no_longer_used(perm_env):
    """命令级 target_env 不再参与裁决（单一裁决源：目标实体）。"""
    perm_env("test", matrix=T2)
    # 无 token 命令 → 本机 test（restart execute）；target_env=prod 被忽略
    assert check_ops_command_permission("systemctl restart myapp", target_env="prod") is None
    # 有 token → 按 token 实体；target_env 亦不改写
    assert check_ops_command_permission("kubectl get pods", target_env="prod") is None


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


def test_matrix_missing_denies_readonly(perm_env):
    """batch83-fix（OPS-DELTA #99）验收 a：矩阵缺失 → 任何命令（含只读）都返回
    deny（fail-closed），不再走 OPS-DELTA #76 惰性放行（None）；错误信息带
    修复指引（先跑 vigil setup / ops-init）。"""
    perm_env("test")  # enabled 缺省 true（_cfg 默认 enabled=True），不写 matrix.yaml
    decision = check_ops_command_permission("ls -la")
    assert decision is not None
    assert decision["action"] == "deny"
    assert decision["matrix_missing"] is True
    assert "matrix.yaml" in decision["description"]
    assert "vigil setup" in decision["description"]


def test_matrix_missing_denies_high_risk_change(perm_env):
    """batch83-fix（OPS-DELTA #99）验收 b：矩阵缺失时高危变更（npm install /
    kubectl delete）同样 deny——不静默放行，也走不到目标解析（矩阵缺失先于
    目标解析，这是 batch83 验收发现的原始漏洞点）。"""
    perm_env("test")
    for cmd in ("npm install -g codex", "kubectl delete pod myapp-0"):
        decision = check_ops_command_permission(cmd)
        assert decision is not None
        assert decision["action"] == "deny"
        assert decision["matrix_missing"] is True
        assert decision["action_name"], cmd


def test_matrix_missing_disabled_gate_returns_none(perm_env):
    """显式 ops.permissions.enabled: false → 矩阵缺失不报错（权限系统关闭，
    交回既有检查，与 terminal 一致）。"""
    perm_env("test", enabled=False)  # 无 matrix.yaml
    assert check_ops_command_permission("ls -la") is None
    assert check_ops_command_permission("npm install -g codex") is None
