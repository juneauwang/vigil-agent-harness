"""操作矩阵接入真实命令守卫（tools/approval.check_all_command_guards）。

YAPL P5（OPS-DELTA #75）：L1-L4 命令分级退役——terminal 直跑的命令过操作分类层
（classifier）归动作枚举，再按动作 × env 查操作矩阵（同一 matrix.yaml 与 runbook
路径）。矩阵无 deny：execute 直接过 / approve 走审批门 / {approve: required}
强制人工（yolo、smart-approval 都不能绕过）。unknown 动作 → 矩阵漏配 → 默认
approve（保守，不是 deny）。命令目标级 env 判定（跨环境硬约束）保留：
test 会话对 prod 实体（node2）的 ssh/命令按 prod 矩阵判定。

无条件层顺序保留：硬底线 / sudo stdin / 用户 deny / ansible inventory guard
仍先于矩阵检查（yolo/mode=off 不可绕过）。
"""

from __future__ import annotations

import yaml

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

# 测试矩阵：prod 变更动作全部 {approve: required}（同 template2 prod 档）；
# test/local 查询与常规动作 execute；bare_metal_prod 走精确名（install=approve
# 供 smart 放行测试）。
GUARD_MATRIX = {
    "prod": {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "restart": {"approve": "required"}, "deploy": {"approve": "required"},
        "run_script": {"approve": "required"}, "reboot": {"approve": "required"},
        "remove": {"approve": "required"}, "stop": {"approve": "required"},
        "install": "approve",
    },
    "test": {
        "query": "execute", "fetch_log": "execute", "restart": "execute",
        "run_script": "execute", "deploy": "execute", "stop": "execute",
    },
    "local": {
        "query": "execute", "restart": "execute", "run_script": "execute",
        "deploy": "execute", "stop": "execute", "install": "execute",
    },
    "dev": {"query": "execute", "restart": "approve", "run_script": "approve"},
    "bare_metal_prod": {
        "query": "execute", "restart": "approve", "install": "approve",
        "run_script": {"approve": "required"},
    },
}


def _write_matrix(home, matrix=None) -> None:
    matrix = matrix if matrix is not None else GUARD_MATRIX
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


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    _session_token = approval_module.set_current_session_key(SESSION)

    def _activate(env="prod", *, enabled=True, environments=None, matrix=None):
        env_yaml = ""
        if environments:
            env_yaml = "  environments:\n" + "\n".join(
                "    - {name: %s, isolation: %s, role: %s}"
                % (e["name"], e["isolation"], e["role"])
                for e in environments
            ) + "\n"
        enabled_yaml = (
            "" if enabled is None else f"    enabled: {str(enabled).lower()}\n"
        )
        (tmp_path / "config.yaml").write_text(
            "ops:\n"
            + env_yaml
            + "  permissions:\n"
            + enabled_yaml
            + f"    env: {env}\n"
            + f"    role: {env}\n"
            + "approvals:\n"
            + "  mode: manual\n"
            + "  timeout: 5\n",
            encoding="utf-8",
        )
        (tmp_path / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
        (tmp_path / "entities").mkdir(exist_ok=True)
        (tmp_path / "entities" / "node2.yaml").write_text(NODE2_YAML, encoding="utf-8")
        (tmp_path / "entities" / "test-web.yaml").write_text(TESTWEB_YAML, encoding="utf-8")
        _write_matrix(tmp_path, matrix=matrix)
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
    with approval_module._lock:
        approval_module._gateway_queues.pop(SESSION, None)
        approval_module._gateway_notify_cbs.pop(SESSION, None)


def _ask_env(monkeypatch):
    monkeypatch.setenv("VIGIL_EXEC_ASK", "1")
    monkeypatch.setenv("VIGIL_GATEWAY_SESSION", "1")


def _register_gateway_auto_approve():
    """注册 gateway 回调：pending 条目自动按 once 批准（模拟用户在弹窗点批准）。"""
    def _notify(_approval_data):
        approval_module.resolve_gateway_approval(SESSION, "once")

    with approval_module._lock:
        approval_module._gateway_notify_cbs[SESSION] = _notify


def test_required_is_hard_to_bypass_even_with_yolo(guard_env, monkeypatch):
    """{approve: required}（restart × prod）→ yolo 也不能绕过（强制人工）。"""
    guard_env("prod")
    _ask_env(monkeypatch)
    approval_module._YOLO_MODE_FROZEN = True
    _register_gateway_auto_approve()
    try:
        result = approval_module.check_all_command_guards("systemctl restart myapp", "local")
        assert result["approved"] is True
        assert result.get("user_approved") is True  # 经人工门，不是 yolo 裸放行
        assert result.get("smart_approved") is None
    finally:
        with approval_module._lock:
            approval_module._gateway_notify_cbs.pop(SESSION, None)


def test_required_forces_human_even_with_smart_approval(guard_env, monkeypatch):
    """prod 变更动作（systemctl restart / ansible-playbook / docker compose up /
    kubectl rollout restart）即使 smart 判 approve 也必须人工确认。"""
    guard_env("prod")
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")
    _register_gateway_auto_approve()
    try:
        for cmd in ("systemctl restart myapp", "ansible-playbook site.yml",
                    "docker compose up -d", "kubectl rollout restart deploy/x"):
            result = approval_module.check_all_command_guards(cmd, "local")
            assert result["approved"] is True, cmd
            assert result.get("user_approved") is True, cmd   # 走人工门
            assert result.get("smart_approved") is None, cmd  # required 覆盖 smart
    finally:
        with approval_module._lock:
            approval_module._gateway_notify_cbs.pop(SESSION, None)


def test_unknown_defaults_to_approve_not_deny(guard_env, monkeypatch):
    """B' 退役（OPS-DELTA #75）：prod 未分级命令（mv/cp）不再是强制确认门——
    unknown → 矩阵漏配 → 默认 approve（走普通审批门，smart 可判）。"""
    guard_env("prod")
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")

    for cmd in ("mv a.txt b.txt", "cp x y"):
        result = approval_module.check_all_command_guards(cmd, "local")
        assert result["approved"] is True, cmd
        assert result.get("smart_approved") is True, cmd  # 普通 approve 档
        assert result.get("user_approved") is None, cmd


def test_approve_fails_closed_without_human(guard_env, monkeypatch):
    """矩阵 approve 需求无人在场（cron/batch）→ fail-closed。"""
    guard_env("prod")
    monkeypatch.delenv("VIGIL_EXEC_ASK", raising=False)
    monkeypatch.delenv("VIGIL_INTERACTIVE", raising=False)

    result = approval_module.check_all_command_guards("systemctl restart myapp", "local")
    assert result["approved"] is False
    assert "no interactive user or gateway" in result["message"]


def test_gate_disabled_leaves_existing_flow_unchanged(guard_env, monkeypatch):
    guard_env("prod", enabled=False)
    monkeypatch.setenv("VIGIL_EXEC_ASK", "1")
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "manual"})

    result = approval_module.check_all_command_guards("pip install requests", "local")
    assert result["approved"] is True


def test_execute_level_passes_directly(guard_env, monkeypatch):
    """query × prod → execute → 直接过（词面绕过消失：docker ps | grep 也 query）。"""
    guard_env("prod")
    for cmd in ("docker ps", "docker ps | grep harbor", "kubectl get pods"):
        result = approval_module.check_all_command_guards(cmd, "local")
        assert result["approved"] is True, cmd
        assert "ops_matrix" not in result, cmd


def test_target_prod_ssh_required_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@203.0.113.11 'rm -rf /var/log' → 目标 node2 (prod)：
    ssh → run_script → prod required（不再按 L3 deny——deny 已退役）。"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.11 'rm -rf /var/log'", "local")
    assert result["approved"] is False  # 无人在场 fail-closed
    assert result["ops_matrix"]["action_name"] == "run_script"
    assert result["ops_matrix"]["level"] == "required"
    assert result["ops_matrix"]["env"] == "prod"
    assert result["ops_matrix"]["require_confirmation"] is True
    assert "目标: node2 (prod)" in result["message"]


def test_target_test_executes_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@<test-web ip> 'rm -rf x' → 目标 test → run_script execute。"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.12 'rm -rf x'", "local")
    assert result["approved"] is True


def test_target_prod_ssh_query_requires_confirmation_in_test_session(guard_env, monkeypatch):
    """test 会话 + ssh root@203.0.113.10 'df -h' → 目标 prod → run_script required
    （受控通道按动作走矩阵，不再因"ssh 包装未分级"进 B' 确认门）。"""
    guard_env("test")
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.10 'df -h'", "local")
    assert result["approved"] is False  # 无人在场 fail-closed
    assert result["ops_matrix"]["action_name"] == "run_script"
    assert result["ops_matrix"]["require_confirmation"] is True
    assert result["ops_matrix"]["env"] == "prod"
    assert "no interactive user" in result["message"]


def test_no_target_keeps_session_env_behavior(guard_env, monkeypatch):
    """无目标命令 → 完全走会话 env：test 查询（docker ps）execute 放行；
    ls 等识别不出的命令 → unknown → 默认 approve（无人在场 fail-closed，保守）。"""
    guard_env("test")
    assert approval_module.check_all_command_guards("docker ps", "local")["approved"] is True
    result = approval_module.check_all_command_guards("ls -la", "local")
    assert result["approved"] is False
    assert result["ops_matrix"]["action_name"] == "unknown"


def test_gate_disabled_target_zero_impact(guard_env, monkeypatch):
    """ops.permissions.enabled=false → 目标解析零影响（ssh 到 prod 实体也放行）。"""
    guard_env("test", enabled=False)
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.11 'rm -rf /var/log'", "local")
    assert result["approved"] is True


def test_target_prod_approve_notes_entity(guard_env, monkeypatch):
    """目标 env=prod 且矩阵需要审批 → 审批提示注明目标实体 + 动作键（action 迁移）。"""
    guard_env("test")
    monkeypatch.setenv("VIGIL_EXEC_ASK", "1")
    monkeypatch.setattr(approval_module, "_get_approval_config",
                        lambda: {"mode": "manual", "timeout": 0.05, "timeout_policy": "deny"})

    notified = []
    approval_module.register_gateway_notify(SESSION, lambda data: notified.append(data))
    try:
        result = approval_module.check_all_command_guards(
            "ssh root@203.0.113.11 'systemctl restart myapp'", "local")
        assert result["approved"] is False  # 超时 = 未授权，但审批提示已发出
        assert len(notified) == 1
        assert "目标: node2 (prod)" in notified[0]["description"]
        # 审批键已从 ops_matrix:{grade}:{env} 迁移到 ops_matrix:{action}:{env}
        assert "ops_matrix:run_script:prod" in notified[0]["pattern_keys"]
    finally:
        approval_module.unregister_gateway_notify(SESSION)


def test_custom_env_role_prod_unknown_still_approve(guard_env):
    """自定义 env bare_metal_prod（role=prod）→ unknown 动作默认 approve（非 deny）。"""
    guard_env("bare_metal_prod", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
        {"name": "local", "isolation": "relaxed", "role": "test"},
    ])
    result = approval_module.check_all_command_guards("rm -rf /var/log", "local")
    assert result["approved"] is False  # 无人在场 fail-closed
    assert result["ops_matrix"]["action"] == "approve"
    assert result["ops_matrix"]["action_name"] == "unknown"
    assert result["ops_matrix"]["env"] == "bare_metal_prod"


def test_custom_env_role_prod_approves_l2(guard_env, monkeypatch):
    """自定义 env bare_metal_prod → 矩阵精确名：只读 approve 档 smart 可判；
    prod 变更动作（install）另走 batch80 硬门强制人工（不 smart 自动批）。"""
    guard_env("bare_metal_prod", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
    ], matrix={"bare_metal_prod": {"query": "approve", "install": "approve"}})
    _ask_env(monkeypatch)
    monkeypatch.setattr(approval_module, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *_: "approve")

    result = approval_module.check_all_command_guards("docker ps", "local")
    assert result["approved"] is True
    assert result["smart_approved"] is True  # 只读 approve 档 smart 可自动放行

    # prod 变更（install）→ 硬门强制人工：smart 判 approve 也不自动放行
    _register_gateway_auto_approve()
    try:
        result = approval_module.check_all_command_guards("pip install requests", "local")
        assert result["approved"] is True
        assert result.get("user_approved") is True
        assert result.get("smart_approved") is None
    finally:
        with approval_module._lock:
            approval_module._gateway_notify_cbs.pop(SESSION, None)


def test_custom_env_role_test_executes(guard_env):
    """自定义 env local（role=test）→ 矩阵精确名：restart execute 直接过。"""
    guard_env("local", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
        {"name": "local", "isolation": "relaxed", "role": "test"},
    ])
    result = approval_module.check_all_command_guards("systemctl restart myapp", "local")
    assert result["approved"] is True
    assert "ops_matrix" not in result


def test_undeclared_env_maps_dev_tier(guard_env):
    """已定义列表之外的 env（staging）→ 档位 dev → dev 矩阵行：query execute。"""
    guard_env("staging", environments=[
        {"name": "bare_metal_prod", "isolation": "strict", "role": "prod"},
    ])
    result = approval_module.check_all_command_guards("docker ps", "local")
    assert result["approved"] is True


def test_matrix_default_enabled_without_enabled_key(guard_env):
    """OPS-DELTA #1：矩阵默认启用——config 只写 env 不写 enabled 也按矩阵判定。"""
    guard_env("prod", enabled=None)
    result = approval_module.check_all_command_guards("rm -rf /var/log", "local")
    assert result["approved"] is False  # unknown → approve → 无人在场 fail-closed
    assert result["ops_matrix"]["action"] == "approve"
    assert result["ops_matrix"]["env"] == "prod"


def test_matrix_inert_without_env(guard_env):
    """矩阵默认启用但未配置 env（非 ops profile）→ 惰性，不改变既有判定。"""
    guard_env("", enabled=None)
    result = approval_module.check_all_command_guards("rm -rf /var/log", "local")
    assert result["approved"] is True
    assert "ops_matrix" not in result
