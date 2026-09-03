"""batch83（OPS-DELTA #98）：操作裁决目标化——矩阵按目标实体 env 裁决。

验收场景（任务书 a-e，逐条实测）：
  a. ssh prod-host 高危变更 → 目标 = prod-host 拓扑实体 → env=prod → prod 矩阵
     裁决（test 会话下执行也一样按 prod，与会话声明 env 无关）；
  b. 本机（拓扑已登记，实体 env=dev）npm install -g codex → dev 矩阵 install
     {approve: required} → require_confirmation（人工确认，不是 smart 自动批）；
  c. 本机 rm -f → 目标 = 本机实体 → 按实体 env 矩阵裁决；本机未登记拓扑 → 高危
     变更解析失败 → 直接 deny + 报错提示先 topo 登记；
  d. ssh 未登记主机跑变更 → 解析失败 → deny；
  e. ls/cat/grep/topo_query 等只读命令 → 不触发目标解析，正常执行。
核心：目标实体驱动，会话声明 env 零参与（高危变更）。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module
from tools.ops_permissions import check_ops_command_permission as ck

SESSION = "batch83-target"

TOPOLOGY = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: dev, isolation: relaxed, role: dev}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443"}
hosts:
  - {name: node2, type: host, env: prod, cluster: k3s-prod, endpoint: "203.0.113.11", detail: entities/node2.yaml}
  - {name: workstation, type: host, env: dev, endpoint: "192.168.1.50", detail: entities/workstation.yaml}
"""

NODE2_YAML = """\
name: node2
type: host
env: prod
attrs:
  public_ip: 203.0.113.12
"""

WORKSTATION_YAML = """\
name: workstation
type: host
env: dev
attrs:
  internal_ip: 192.168.1.50
"""

MATRIX = {
    "prod": {
        "query": "execute", "fetch_log": "execute", "verify": "execute",
        "run_script": {"approve": "required"},
        "install": {"approve": "required"}, "remove": {"approve": "required"},
        "decommission": {"approve": "required"},
        "restart": "approve",
    },
    "dev": {
        "query": "execute",
        "install": {"approve": "required"}, "remove": "approve",
        "run_script": "approve",
    },
    "test": {
        "query": "execute", "remove": "approve", "run_script": "execute",
    },
}


def _write_matrix(home, matrix=None) -> None:
    matrix = matrix if matrix is not None else MATRIX
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-08-30T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {
            env: {act: "test" for act in cells} for env, cells in matrix.items()
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def t83(tmp_path, monkeypatch):
    _session_token = approval_module.set_current_session_key(SESSION)

    def _activate(env="test", *, topology=True, matrix=None, local="workstation"):
        (tmp_path / "config.yaml").write_text(
            "ops:\n  permissions:\n    enabled: true\n"
            f"    env: {env}\n    role: {env}\n"
            "approvals:\n  mode: manual\n  timeout: 5\n",
            encoding="utf-8",
        )
        if topology:
            (tmp_path / "topology.yaml").write_text(TOPOLOGY, encoding="utf-8")
            (tmp_path / "entities").mkdir(exist_ok=True)
            (tmp_path / "entities" / "node2.yaml").write_text(NODE2_YAML, encoding="utf-8")
            (tmp_path / "entities" / "workstation.yaml").write_text(
                WORKSTATION_YAML, encoding="utf-8")
        _write_matrix(tmp_path, matrix=matrix)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        approval_module._YOLO_MODE_FROZEN = False
        approval_module.clear_session(SESSION)
        approval_module._permanent_approved.clear()
        if local is not None:
            monkeypatch.setattr(
                "tools.target_resolve._local_host_identities",
                lambda: [local],
            )
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    approval_module.clear_session(SESSION)
    approval_module._permanent_approved.clear()
    approval_module.reset_current_session_key(_session_token)
    with approval_module._lock:
        approval_module._gateway_queues.pop(SESSION, None)
        approval_module._gateway_notify_cbs.pop(SESSION, None)


def test_a_ssh_prod_host_high_risk_judged_by_prod_matrix(t83):
    """test 会话 + ssh prod-host 高危变更 → 目标 node2 (prod) → prod 矩阵裁决
    （与会话声明 env=test 无关）。"""
    t83("test")
    for cmd in ("ssh root@node2 'kubectl delete ns foo'",
                "ssh root@node2 'npm install -g codex'"):
        decision = ck(cmd)
        assert decision is not None, cmd
        assert decision["action"] == "approve", cmd
        assert decision["action_name"] == "run_script", cmd
        assert decision["env"] == "prod", cmd
        assert decision["level"] == "required", cmd
        assert decision["require_confirmation"] is True, cmd
        assert decision.get("target_label") == "node2 (prod)", cmd


def test_b_local_install_uses_local_host_env_and_requires_confirmation(t83):
    """本机（拓扑已登记，实体 env=dev）npm install → 目标=本机实体 → dev 矩阵
    install {approve: required} → require_confirmation（会话 env=test 零参与）。"""
    t83("test")
    decision = ck("npm install -g codex")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["action_name"] == "install"
    assert decision["env"] == "dev"
    assert decision["level"] == "required"
    assert decision["require_confirmation"] is True
    assert decision.get("target_label") == "workstation (dev)"


def test_c_local_rm_judged_by_entity_env_then_deny_unregistered(t83):
    """本机 rm -f → 目标=本机实体 → 实体 env 矩阵裁决；本机未登记拓扑 → deny。"""
    t83("test")
    decision = ck("rm -f /tmp/x")
    assert decision is not None
    assert decision["action_name"] == "remove"
    assert decision["env"] == "dev"          # 实体 env=dev，非会话 test
    assert decision["level"] == "approve"

    # 本机未登记拓扑 → 高危变更解析失败 → 直接 deny（不是 confirm）
    t83("test", local="ghost-machine")
    decision = ck("rm -f /tmp/x")
    assert decision is not None
    assert decision["action"] == "deny"
    assert decision["target_deny"] is True
    assert "操作已拒绝" in decision["description"]
    assert "topo_query" in decision["description"]
    assert decision["require_confirmation"] is False


def test_d_ssh_unregistered_host_change_denied(t83):
    """ssh 未登记主机跑变更 → 解析失败 → deny（不再借会话 env 兜底）。"""
    t83("test")
    decision = ck("ssh root@203.0.113.99 'rm -rf /var/log'")
    assert decision is not None
    assert decision["action"] == "deny"
    assert "未在拓扑表登记" in decision["description"]
    assert "topo_query" in decision["description"]


def test_e_readonly_commands_skip_target_resolution(t83):
    """只读/无害命令不触发目标解析：拓扑缺失也不 deny，按矩阵只读格子走。
    batch86（OPS-DELTA #102）内置只读种子表后：ls/cat 等种子形态归一命中 →
    直接放行（None，跳过审批门）；未登记只读形态（grep 带模式/路径操作数、
    topo_query）仍走 unknown→approve 原判定——两者都不产生目标解析 deny。"""
    t83("test", topology=False)
    assert ck("docker ps") is None          # query × test = execute → 直接过
    assert ck("kubectl get pods") is None
    assert ck("ls -la") is None             # 种子 ls：归一命中 → 直接执行
    assert ck("cat /etc/hosts") is None     # 种子 cat 读形态 → 直接执行
    for cmd in ("grep -r foo /etc", "topo_query"):
        decision = ck(cmd)
        assert decision is not None, cmd
        assert decision["action"] == "approve", cmd
        assert decision["action_name"] == "unknown", cmd
        assert decision.get("target_deny") is not True, cmd


def test_deny_is_hard_not_bypassed_by_yolo_or_mode_off(t83, monkeypatch):
    """deny 是目标解析层硬拦截：yolo / approvals.mode=off 都不能放行。"""
    t83("test", local="ghost-machine")
    monkeypatch.setenv("VIGIL_SESSION_KEY", "batch83-deny-hard")
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    result = approval_module.check_all_command_guards("npm install -g codex", "local")
    assert result["approved"] is False
    assert result.get("ops_target_deny") is True
    assert "操作已拒绝" in result["message"]
    assert "topo_query" in result["message"]


def test_b_e2e_required_confirmation_rides_smart_approval(t83, monkeypatch):
    """E2E：dev install {approve: required} → smart approve 被降级为人工确认，
    审批提示带目标实体（workstation (dev)）。"""
    t83("test")
    monkeypatch.setenv("VIGIL_SESSION_KEY", "batch83-b-e2e")
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_module, "_smart_approve", lambda *a, **k: "approve")
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )

    seen = {"prompts": 0, "descriptions": []}

    def _cb(command, description, **kw):
        seen["prompts"] += 1
        seen["descriptions"].append(description)
        return "once"

    token = approval_module.set_hermes_interactive_context(True)
    try:
        result = approval_module.check_all_command_guards(
            "npm install -g codex", "local", approval_callback=_cb)
    finally:
        approval_module.reset_hermes_interactive_context(token)
        try:
            from tools.credential_vault import _REGISTERED
            _REGISTERED.pop("__user_authorized__", None)
        except Exception:
            pass

    assert result["approved"] is True
    assert "smart_approved" not in result, result
    assert result.get("user_approved") is True
    assert seen["prompts"] == 1, seen
    assert any("install" in d and "dev" in d for d in seen["descriptions"]), seen
    assert any("workstation (dev)" in d for d in seen["descriptions"]), seen


def test_low_risk_change_keeps_session_env_transitional(t83):
    """低危变更（restart/upgrade 等）首批暂用会话 env 裁决（过渡态，不强制解析）。"""
    t83("test")
    decision = ck("systemctl restart myapp")
    assert decision is not None
    assert decision["action_name"] == "restart"
    assert decision["env"] == "test"         # 低危变更暂用会话 env（过渡态）
    assert decision["require_confirmation"] is False
