"""Ops permission matrix — command grade × environment (ops-agent-harness.md §3)."""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import (
    check_ops_command_permission,
    classify_command,
)


def _cfg(tmp_path, env, *, enabled=True, extra="") -> str:
    (tmp_path / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    env: {env}\n"
        "    role: operator\n"
        f"{extra}",
        encoding="utf-8",
    )


@pytest.fixture
def perm_env(tmp_path, monkeypatch):
    def _activate(env, *, enabled=True, extra=""):
        _cfg(tmp_path, env, enabled=enabled, extra=extra)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    from tools import ops_permissions as _op
    _op._WARNED_ENVS.clear()


def test_grades_L1_query(perm_env):
    perm_env("test")
    assert classify_command("ls -la") == "L1"
    assert classify_command("df -h /data") == "L1"
    assert classify_command("systemctl status nginx") == "L1"
    assert classify_command("kubectl get pods -n prod") == "L1"


def test_grades_L1_rejects_shell_operators(perm_env):
    perm_env("test")
    # 带操作符/重定向/破坏性选项 → 不是纯查询
    assert classify_command("ls | grep x") is None
    assert classify_command("echo x > /etc/hosts") is None
    assert classify_command("find / -delete") is None
    assert classify_command("curl -X POST http://x/api") is None


def test_grades_L2_regular(perm_env):
    perm_env("test")
    assert classify_command("systemctl restart myapp") == "L2"
    assert classify_command("pip install requests") == "L2"
    assert classify_command("apt install nginx") == "L2"
    assert classify_command("kubectl rollout restart deploy/myapp") == "L2"
    assert classify_command("docker compose up -d") == "L2"


def test_grades_L3_dangerous(perm_env):
    perm_env("test")
    assert classify_command("iptables -F") == "L3"
    assert classify_command("systemctl restart mysql") == "L3"
    assert classify_command("sed -i s/x/y/g /etc/app.conf") == "L3"
    assert classify_command("kubectl apply -f deploy.yaml") == "L3"


def test_grades_L4_fatal(perm_env):
    perm_env("test")
    assert classify_command("kubectl delete namespace prod") == "L4"
    assert classify_command("kubectl delete ns prod") == "L4"
    assert classify_command("DROP DATABASE orders") == "L4"
    assert classify_command("mkfs.ext4 /dev/sdb1") == "L4"


def test_matrix_test_env_executes_everything(perm_env):
    perm_env("test")
    for cmd in ("ls -la", "systemctl restart myapp", "iptables -F", "mkfs.ext4 /dev/x"):
        assert check_ops_command_permission(cmd) is None, cmd


def test_matrix_uat_maps_to_prod_tier(perm_env):
    """老 env=uat → 映射 prod 档（更严不更松）：L2 审批 / L3、L4 拒绝。"""
    perm_env("uat")
    assert check_ops_command_permission("ls -la") is None
    approve = check_ops_command_permission("systemctl restart myapp")
    assert approve["action"] == "approve"
    assert approve["grade"] == "L2"
    assert approve["env_tier"] == "prod"
    assert check_ops_command_permission("iptables -F")["action"] == "deny"
    assert check_ops_command_permission("kubectl delete ns prod")["action"] == "deny"


def test_matrix_prod_approves_l2_denies_l3_l4(perm_env):
    perm_env("prod")
    assert check_ops_command_permission("ls -la") is None
    approve = check_ops_command_permission("systemctl restart myapp")
    assert approve["action"] == "approve"
    assert approve["grade"] == "L2" and approve["env"] == "prod"

    deny_l3 = check_ops_command_permission("iptables -F")
    assert deny_l3["action"] == "deny" and deny_l3["grade"] == "L3"
    deny_l4 = check_ops_command_permission("kubectl delete namespace prod")
    assert deny_l4["action"] == "deny" and deny_l4["grade"] == "L4"


def test_disabled_gate_returns_none(perm_env):
    perm_env("prod", enabled=False)
    assert check_ops_command_permission("kubectl delete namespace prod") is None


def test_unknown_env_maps_to_dev_tier(perm_env):
    """四值外新自定义名按档位推导（默认 dev 档 → L3 直接放行，仍返回 None）。"""
    perm_env("edge")
    assert check_ops_command_permission("iptables -F") is None
    from tools.ops_permissions import _map_env_tier
    assert _map_env_tier("edge") == "dev"


def test_non_string_or_empty_returns_none(perm_env):
    perm_env("prod")
    assert check_ops_command_permission("") is None
    assert check_ops_command_permission(None) is None


def test_custom_matrix_override(perm_env):
    perm_env("prod", extra="    matrix:\n      prod: {L1: execute, L2: deny, L3: deny, L4: deny}\n")
    result = check_ops_command_permission("systemctl restart myapp")
    assert result is not None and result["action"] == "deny"
    assert result["grade"] == "L2"


def test_custom_grade_override(perm_env):
    perm_env("prod", extra="    grades:\n      L2:\n        - \"customctl .*\"\n")
    result = check_ops_command_permission("customctl reload")
    assert result is not None and result["action"] == "approve"
    assert result["grade"] == "L2"


def test_target_env_overrides_session_env(perm_env):
    perm_env("test")
    # test 会话 + 目标 prod → 按 prod 矩阵判定
    deny = check_ops_command_permission("iptables -F", target_env="prod")
    assert deny is not None and deny["action"] == "deny"
    assert deny["env"] == "prod" and deny["grade"] == "L3"

    approve = check_ops_command_permission("systemctl restart myapp", target_env="prod")
    assert approve is not None and approve["action"] == "approve"
    assert approve["env"] == "prod" and approve["grade"] == "L2"

    # test 会话 + 目标 test → test 矩阵（L3 直接执行）
    assert check_ops_command_permission("rm -rf /tmp/x", target_env="test") is None


def test_target_env_matches_session_env(perm_env):
    perm_env("prod")
    deny = check_ops_command_permission("iptables -F", target_env="prod")
    assert deny is not None and deny["action"] == "deny" and deny["env"] == "prod"


def test_unknown_target_env_maps_to_dev_tier(perm_env):
    perm_env("test")
    assert check_ops_command_permission("iptables -F", target_env="edge") is None


def test_target_env_with_gate_disabled_returns_none(perm_env):
    perm_env("prod", enabled=False)
    assert check_ops_command_permission("kubectl delete namespace prod", target_env="prod") is None


def test_target_env_empty_falls_back_to_session_env(perm_env):
    perm_env("prod")
    result = check_ops_command_permission("systemctl restart myapp", target_env="")
    assert result is not None and result["action"] == "approve" and result["env"] == "prod"

# ---------------------------------------------------------------------------
# OPS-DELTA #42：env 四值枚举 + 老自定义名档位映射
# ---------------------------------------------------------------------------

def test_legacy_env_mapping_warns_once(perm_env):
    import logging
    from tools import ops_permissions as op

    perm_env("uat")
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
    mapping_warns = [m for m in records if "uat" in m and "映射" in m]
    assert len(mapping_warns) == 1


def test_legacy_env_tier_mapping_rules(perm_env):
    from tools.ops_permissions import _map_env_tier

    perm_env("test")
    # 四值原样返回。
    assert _map_env_tier("local") == "local"
    assert _map_env_tier("test") == "test"
    assert _map_env_tier("dev") == "dev"
    assert _map_env_tier("prod") == "prod"
    # 老自定义名：uat→prod、staging→dev。
    assert _map_env_tier("uat") == "prod"
    assert _map_env_tier("staging") == "dev"
    # 其余按名字推导：含 prod → prod；尾缀 local/test/dev → 对应档；默认 dev。
    assert _map_env_tier("bare_metal_prod") == "prod"
    assert _map_env_tier("cloud") == "dev"


def test_legacy_config_env_definition_isolation_drives_tier(perm_env):
    """老配置带 isolation 定义：strict → prod 档、relaxed → dev 档（role 优先）。"""
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

    # config 只声明老自定义名 uat → 映射为 prod 档（/env 名单同步收敛）。
    perm_env("test", extra=(
        "  environments:\n"
        "    - {name: uat, isolation: strict, role: uat}\n"
    ))
    envs = defined_environments()
    assert [d["name"] for d in envs] == ["prod"]
    assert envs[0]["isolation"] == "strict"
