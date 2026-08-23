"""操作矩阵 {approve: required} 强制人工确认门（YAPL P5，OPS-DELTA #75）。

验收点（打在 tools.ops_permissions.check_ops_command_permission 上）：
  - prod env 下变更类命令（systemctl restart / ansible-playbook / docker
    compose up / kubectl rollout restart 等）→ 动作进 prod 矩阵 required 档 →
    require_confirmation=true（强制人工，覆盖 approvals.mode）；
  - 识别不出的变更类命令（kubectl edit/drain/cordon 不在规则表）→ unknown →
    默认 approve（require_confirmation=false，不再是 B' 强制确认门）；
  - test env 下同类命令 → 矩阵 execute → None（不触发确认门）；
  - prod env 下查询动作（kubectl get / docker ps / systemctl status）→ execute
    → None（不受影响）；
  - ops.permissions.enabled=false → 全部零影响（None）。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import check_ops_command_permission

# prod 矩阵（同 template2 prod 档）：变更/高危动作全部 {approve: required}。
PROD_REQUIRED = [
    "restart", "start", "stop", "reload", "upgrade", "install", "scale",
    "run_script", "deploy", "rollback", "restore", "reboot", "shutdown",
    "remove", "decommission",
]
PROD_APPROVE = ["apply_config", "backup", "transfer_file", "enable", "disable"]
PROD_EXECUTE = ["query", "fetch_log", "verify"]

MATRIX = {
    "prod": {
        **{a: "execute" for a in PROD_EXECUTE},
        **{a: "approve" for a in PROD_APPROVE},
        **{a: {"approve": "required"} for a in PROD_REQUIRED},
    },
    "test": {
        **{a: "execute" for a in PROD_EXECUTE},
        **{a: "execute" for a in PROD_REQUIRED},
        **{a: "execute" for a in PROD_APPROVE},
    },
    "local": {
        **{a: "execute" for a in PROD_EXECUTE},
        **{a: "execute" for a in PROD_REQUIRED},
        **{a: "execute" for a in PROD_APPROVE},
    },
}


def _write_matrix(home) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-23T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": MATRIX,
        "sources": {
            env: {act: "test" for act in cells} for env, cells in MATRIX.items()
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _cfg(tmp_path, env, *, enabled=True) -> None:
    (tmp_path / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    env: {env}\n"
        f"    role: {env}\n",
        encoding="utf-8",
    )
    _write_matrix(tmp_path)


@pytest.fixture
def perm_env(tmp_path, monkeypatch):
    def _activate(env, *, enabled=True):
        _cfg(tmp_path, env, enabled=enabled)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()


# 变更类命令 → prod 矩阵 required（强制人工确认门）。
REQUIRED_COMMANDS = [
    "ansible-playbook site.yml",
    "kubectl apply -f x.yaml",
    "kubectl delete deploy x",
    "kubectl scale deploy x --replicas=2",
    "kubectl rollout restart deploy/x",
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

# 变更类但 classifier 识别不出（规则表外）→ unknown → 默认 approve（非强制）。
UNKNOWN_APPROVE_COMMANDS = [
    "kubectl edit deploy x",
    "kubectl drain node1",
    "kubectl cordon node2",
]


@pytest.mark.parametrize("cmd", REQUIRED_COMMANDS)
def test_prod_change_commands_require_confirmation(perm_env, cmd):
    perm_env("prod")
    result = check_ops_command_permission(cmd)
    assert result is not None, cmd
    assert result["action"] == "approve", cmd
    assert result["level"] == "required", cmd
    assert result["require_confirmation"] is True, cmd
    assert "强制人工确认" in result["description"], cmd


@pytest.mark.parametrize("cmd", UNKNOWN_APPROVE_COMMANDS)
def test_prod_unknown_change_commands_default_approve(perm_env, cmd):
    """规则表外的变更类命令（kubectl edit/drain/cordon）→ unknown → 默认 approve
    （B' 强制确认门退役，OPS-DELTA #75：保守 = approve 走审批门，不是 required）。"""
    perm_env("prod")
    result = check_ops_command_permission(cmd)
    assert result is not None, cmd
    assert result["action_name"] == "unknown", cmd
    assert result["level"] == "approve", cmd
    assert result["require_confirmation"] is False, cmd
    assert "未能识别命令意图" in result["description"], cmd


@pytest.mark.parametrize("cmd", REQUIRED_COMMANDS)
def test_test_env_change_commands_execute(perm_env, cmd):
    """test env 下已识别变更命令 → 矩阵 execute → None（不触发确认门）。"""
    perm_env("test")
    assert check_ops_command_permission(cmd) is None, cmd


@pytest.mark.parametrize("cmd", UNKNOWN_APPROVE_COMMANDS)
def test_test_env_unknown_still_default_approve(perm_env, cmd):
    """test env 下识别不出的命令 → unknown → 默认 approve（保守，与 env 无关）。"""
    perm_env("test")
    result = check_ops_command_permission(cmd)
    assert result is not None, cmd
    assert result["action_name"] == "unknown", cmd
    assert result["level"] == "approve", cmd


def test_prod_query_unaffected(perm_env):
    perm_env("prod")
    for cmd in ("kubectl get pods", "kubectl describe deploy x", "docker ps",
                "systemctl status myapp"):
        assert check_ops_command_permission(cmd) is None, cmd


def test_gate_disabled_zero_impact(perm_env):
    perm_env("prod", enabled=False)
    for cmd in REQUIRED_COMMANDS + ["kubectl get pods"]:
        assert check_ops_command_permission(cmd) is None, cmd


def test_uat_maps_prod_tier_triggers_confirmation_gate(perm_env):
    """OPS-DELTA #42：legacy uat → prod 档（更严不更松）→ 变更动作走 prod required。"""
    perm_env("uat")
    result = check_ops_command_permission("systemctl restart myapp")
    assert result is not None
    assert result["env_tier"] == "prod"
    assert result["level"] == "required"
    assert result["require_confirmation"] is True
    assert "强制人工确认" in result["description"]


def test_non_required_approve_in_prod_not_gated(perm_env):
    """prod 下非 required 档 approve（scp → transfer_file / git push → unknown）
    → require_confirmation=false（不是强制确认门）。"""
    perm_env("prod")
    for cmd in ("scp a.txt ops@h:/tmp", "git push origin main"):
        result = check_ops_command_permission(cmd)
        assert result is not None, cmd
        assert result["require_confirmation"] is False, cmd


def test_target_env_prod_triggers_confirmation_in_test_session(perm_env):
    """test 会话 + 目标 prod 实体（跨环境）→ 同样按 prod 判定触发确认门。"""
    perm_env("test")
    result = check_ops_command_permission("systemctl restart myapp", target_env="prod")
    assert result is not None
    assert result["require_confirmation"] is True
    assert result["env"] == "prod"
