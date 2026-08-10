"""Ops permission matrix integrated in the real command guard (tools/approval.py).

包含命令目标级 env 判定（跨环境硬约束二期）：test 会话对 prod 实体（node2）的
L3/L4 命令按 prod 矩阵硬拒绝；对 test 实体按 test 矩阵放行；解析不到目标时
完全走现状（会话 env）。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module

SESSION = "ops-guard-test"

TOPO_YAML = """\
version: 1
sources: [test]
environments:
  - name: prod
    entry: "ssh user@203.0.113.10"
    isolation: strict
    role: prod
    core_entities: [k3s-prod, node1, node2]
  - name: test
    entry: "ssh test-jump"
    isolation: relaxed
    role: test
    core_entities: [test-web]
core_entities:
  - {name: k3s-prod, type: k8s, env: prod, endpoint: "https://203.0.113.15:6443", detail: entities/k3s-prod.yaml}
  - {name: node1, type: k8s-node, env: prod, endpoint: "203.0.113.10", detail: entities/node1.yaml}
  - {name: node2, type: k8s-node, env: prod, detail: entities/node2.yaml}
  - {name: test-web, type: service, env: test, detail: entities/test-web.yaml}
"""

NODE2_YAML = """\
name: node2
type: k8s-node
env: prod
attrs:
  internal_ip: 203.0.113.14
  public_ip: 203.0.113.11
"""

TESTWEB_YAML = """\
name: test-web
type: service
env: test
attrs:
  public_ip: 203.0.113.12
"""


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    approval_module.set_current_session_key(SESSION)

    def _activate(env="prod", *, enabled=True, environments=None):
        env_yaml = ""
        if environments:
            env_yaml = "  environments:\n" + "\n".join(
                "    - {name: %s, isolation: %s, role: %s}"
                % (e["name"], e["isolation"], e["role"])
                for e in environments
            ) + "\n"
        (tmp_path / "config.yaml").write_text(
            "ops:\n"
            + env_yaml
            + "  permissions:\n"
            f"    enabled: {str(enabled).lower()}\n"
            f"    env: {env}\n"
            f"    role: {env}\n",
            encoding="utf-8",
        )
        (tmp_path / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
        (tmp_path / "entities").mkdir(exist_ok=True)
        (tmp_path / "entities" / "node2.yaml").write_text(NODE2_YAML, encoding="utf-8")
        (tmp_path / "entities" / "test-web.yaml").write_text(TESTWEB_YAML, encoding="utf-8")
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


def test_matrix_deny_for_kubectl_prod_regardless_of_session_env(guard_env, monkeypatch):
    guard_env("uat")
    result = approval_module.check_all_command_guards("kubectl delete namespace prod", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action"] == "deny"

    guard_env("test")
    # kubectl 关联 k3s-prod（prod）→ test 会话对 prod 集群的 L4 同样硬拒绝
    result = approval_module.check_all_command_guards("kubectl delete namespace prod", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action"] == "deny"
    assert result["ops_matrix"]["grade"] == "L4"
    assert result["ops_matrix"]["env"] == "prod"


def test_target_prod_l3_deny_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@203.0.113.11 'rm -rf /var/log' → 目标 node2 (prod) L3 拒绝"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.11 'rm -rf /var/log'", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action"] == "deny"
    assert result["ops_matrix"]["grade"] == "L3"
    assert result["ops_matrix"]["env"] == "prod"
    assert "目标: node2 (prod)" in result["message"]


def test_target_prod_l4_deny_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@203.0.113.11 'kubectl delete namespace foo' → L4 拒绝"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.11 'kubectl delete namespace foo'", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["grade"] == "L4"
    assert result["ops_matrix"]["env"] == "prod"


def test_target_test_l3_executes_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@<test-web ip> 'rm -rf x' → 目标 test → L3 直接执行"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.12 'rm -rf x'", "local")
    assert result["approved"] is True


def test_target_prod_l1_executes_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@203.0.113.10 'df -h' → 目标 prod L1 直接执行"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.10 'df -h'", "local")
    assert result["approved"] is True


def test_no_target_keeps_session_env_behavior(guard_env, monkeypatch):
    """无目标命令（ls / docker ps）→ 完全走现状：test 会话全放行"""
    guard_env("test")
    assert approval_module.check_all_command_guards("ls -la", "local")["approved"] is True
    assert approval_module.check_all_command_guards("docker ps", "local")["approved"] is True


def test_gate_disabled_target_zero_impact(guard_env, monkeypatch):
    """ops.permissions.enabled=false → 目标解析零影响（ssh 到 prod 实体也放行）"""
    guard_env("test", enabled=False)
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.11 'rm -rf /var/log'", "local")
    assert result["approved"] is True


def test_target_prod_approve_notes_entity(guard_env, monkeypatch):
    """目标 env=prod 且矩阵 approve → 审批提示注明目标实体（目标: node2 (prod)）"""
    guard_env("test")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval_module, "_get_approval_config",
                        lambda: {"mode": "manual", "timeout": 0.05})

    notified = []
    approval_module.register_gateway_notify(SESSION, lambda data: notified.append(data))
    try:
        result = approval_module.check_all_command_guards(
            "ssh root@203.0.113.11 'systemctl restart myapp'", "local")
        assert result["approved"] is False  # 超时 = 未授权，但审批提示已发出
        assert len(notified) == 1
        assert "目标: node2 (prod)" in notified[0]["description"]
        assert "ops_matrix:L2:prod" in notified[0]["pattern_keys"]
    finally:
        approval_module.unregister_gateway_notify(SESSION)

def test_custom_env_role_prod_denies_l3(guard_env):
    """自定义 env bare_metal_prod（role=prod）→ L3 硬拒绝，矩阵按 role 判定不依赖名字。"""
    guard_env("bare_metal_prod", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
        {"name": "local", "isolation": "relaxed", "role": "test"},
    ])
    result = approval_module.check_all_command_guards("rm -rf /var/log", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action"] == "deny"
    assert result["ops_matrix"]["grade"] == "L3"
    assert result["ops_matrix"]["env"] == "bare_metal_prod"


def test_custom_env_role_prod_approves_l2(guard_env, monkeypatch):
    """自定义 env bare_metal_prod → L2 需要审批（同 prod 档）。"""
    guard_env("bare_metal_prod", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
    ])
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")

    result = approval_module.check_all_command_guards("systemctl restart myapp", "local")
    assert result["approved"] is True
    assert result["smart_approved"] is True


def test_custom_env_role_test_executes_l3(guard_env):
    """自定义 env local（role=test）→ L3 直接执行（同 test 档）。"""
    guard_env("local", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
        {"name": "local", "isolation": "relaxed", "role": "test"},
    ])
    result = approval_module.check_all_command_guards("rm -rf /var/log", "local")
    assert result["approved"] is True


def test_undeclared_env_leaves_existing_flow(guard_env):
    """ops.environments 已定义列表之外的 env → 不做矩阵判定，交回原有检查。"""
    guard_env("staging", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
    ])
    result = approval_module.check_all_command_guards("ls -la", "local")
    assert result["approved"] is True
