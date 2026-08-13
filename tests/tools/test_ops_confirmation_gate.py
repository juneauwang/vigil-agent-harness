"""OPS-DELTA #32 — prod 变更强制确认门（require_confirmation 字段）。

验收点（直接打在 tools.ops_permissions.check_ops_command_permission 上）：
  - prod env 下 ansible-playbook / kubectl apply / docker compose up 等变更类
    命令 → require_confirmation=true，无确认不放行；
  - test/uat env 下同类命令 → 现状不变（矩阵判定为 execute → None）；
  - prod env 下 kubectl get pods（L1 查询）→ 不受影响（None）；
  - ops.permissions.enabled=false → 全部零影响（None）。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import check_ops_command_permission


def _cfg(tmp_path, env, *, enabled=True) -> None:
    (tmp_path / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    env: {env}\n"
        f"    role: {env}\n",
        encoding="utf-8",
    )


@pytest.fixture
def perm_env(tmp_path, monkeypatch):
    def _activate(env, *, enabled=True):
        _cfg(tmp_path, env, enabled=enabled)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()


CHANGE_COMMANDS = [
    "ansible-playbook site.yml",
    "kubectl apply -f x.yaml",
    "kubectl delete deploy x",
    "kubectl edit deploy x",
    "kubectl scale deploy x --replicas=2",
    "kubectl rollout restart deploy/x",
    "kubectl drain node1",
    "kubectl cordon node1",
    "docker compose up -d",
    "docker compose restart",
    "docker compose rm svc",
    "docker compose down",
    "docker restart myapp",
    "docker rm stale-container",
    "docker stop myapp",
    "systemctl restart myapp",
    "systemctl stop myapp",
    "helm upgrade app chart",
    "helm install app chart",
    "helm uninstall app",
]


@pytest.mark.parametrize("cmd", CHANGE_COMMANDS)
def test_prod_change_commands_require_confirmation(perm_env, cmd):
    perm_env("prod")
    result = check_ops_command_permission(cmd)
    assert result is not None, cmd
    assert result["require_confirmation"] is True, cmd
    # deny > 确认门：L3/L4 仍硬拒；approve 才走确认门。
    assert result["action"] in ("approve", "deny"), cmd
    if result["action"] == "approve":
        assert "prod 变更确认门" in result["description"], cmd


@pytest.mark.parametrize("cmd", CHANGE_COMMANDS)
def test_test_env_change_commands_unchanged(perm_env, cmd):
    """test env 下同类命令 → 矩阵 execute → None（现状，不触发确认门）。"""
    perm_env("test")
    assert check_ops_command_permission(cmd) is None, cmd


def test_prod_l1_query_unaffected(perm_env):
    perm_env("prod")
    for cmd in ("kubectl get pods", "kubectl describe deploy x", "docker ps",
                "systemctl status myapp", "ls -la"):
        assert check_ops_command_permission(cmd) is None, cmd


def test_gate_disabled_zero_impact(perm_env):
    perm_env("prod", enabled=False)
    for cmd in CHANGE_COMMANDS + ["kubectl get pods"]:
        assert check_ops_command_permission(cmd) is None, cmd


def test_uat_maps_prod_tier_triggers_confirmation_gate(perm_env):
    """OPS-DELTA #42：legacy uat → prod 档（更严不更松）→ 变更确认门触发。"""
    perm_env("uat")
    result = check_ops_command_permission("systemctl restart myapp")
    assert result is not None
    assert result["env_tier"] == "prod"
    assert result["require_confirmation"] is True
    assert "prod 变更确认门" in result["description"]


def test_non_change_approve_in_prod_not_confirmation_gated(perm_env):
    """prod 下非变更类 approve（git push / pip install）→ require_confirmation=false。"""
    perm_env("prod")
    for cmd in ("git push", "pip install requests", "scp a b@h:/tmp"):
        result = check_ops_command_permission(cmd)
        if result is not None:
            assert result.get("require_confirmation") is False, cmd


def test_target_env_prod_triggers_confirmation_in_test_session(perm_env):
    """test 会话 + 目标 prod 实体（跨环境）→ 同样按 prod 判定触发确认门。"""
    perm_env("test")
    result = check_ops_command_permission("systemctl restart myapp", target_env="prod")
    assert result is not None
    assert result["require_confirmation"] is True
    assert result["env"] == "prod"
