"""task103 任务 3：目标解析失败 → 矩阵 `unresolved` 行（取代 deny）。

- 目标未在拓扑登记（bootstrap / 重建 / 新机发现）→ 人工确认（不再是 BLOCKED）；
  同意即执行；文案含"目标未在拓扑登记——按最高严格度强制人工确认；建议随后
  topo_update 登记（不阻断本次执行）"。
- 未识别动作（如 vigil topo-discover）同样按最高严格度 required。
- matrix.yaml 缺失 → 仍 deny（fail-closed，保留）。

反证：基线 8ba4b5ce 解析失败 → action=deny（assert action=="approve" 失败；
approval 返回 BLOCKED）。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module
from tools.ops_permissions import check_ops_command_permission as ck

SESSION = "t103-unresolved"
MANDATED = "目标未在拓扑登记——按最高严格度强制人工确认；建议随后 topo_update 登记（不阻断本次执行）"

TOPO = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443"}
  - {name: k3s-test, env: test, type: k3s, endpoint: "https://203.0.113.25:6443"}
hosts:
  - {name: workstation, type: host, env: local, endpoint: "192.168.1.50"}
  - {name: node-prod, type: host, env: prod, endpoint: "203.0.113.11"}
"""

MATRIX = {
    "local": {"query": "execute", "run_script": "execute", "install": "execute"},
    "prod": {"query": "execute", "run_script": {"approve": "required"},
             "install": {"approve": "required"}},
    "test": {"query": "execute"},
    "unresolved": {"query": {"approve": "required"},
                   "run_script": {"approve": "required"},
                   "install": {"approve": "required"}},
}


def _write_matrix(home) -> None:
    (home / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-10-07T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": MATRIX,
        "sources": {e: {a: "test" for a in c} for e, c in MATRIX.items()},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    token = approval_module.set_current_session_key(SESSION)

    def _activate(*, local_registered=True, matrix=True):
        topo = TOPO
        if not local_registered:
            topo = TOPO.replace(
                '  - {name: workstation, type: host, env: local, endpoint: "192.168.1.50"}\n',
                "",
            )
        (tmp_path / "topology.yaml").write_text(topo, encoding="utf-8")
        if matrix:
            _write_matrix(tmp_path)
        (tmp_path / "config.yaml").write_text(
            "ops:\n  permissions:\n    enabled: true\n    env: test\n    role: test\n"
            "approvals:\n  mode: manual\n  timeout: 5\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        monkeypatch.setattr(
            "tools.target_resolve._local_host_identities", lambda: ["workstation"])
        hc._LOAD_CONFIG_CACHE.clear()
        approval_module._YOLO_MODE_FROZEN = False
        approval_module.clear_session(SESSION)
        approval_module._permanent_approved.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()
    approval_module.clear_session(SESSION)
    approval_module._permanent_approved.clear()
    approval_module.reset_current_session_key(token)


def test_ssh_unregistered_becomes_confirmation(env_home):
    """ssh 未登记主机 → 人工确认（action=approve + require_confirmation）。"""
    env_home()
    decision = ck("ssh root@203.0.113.99 'df -h'")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["require_confirmation"] is True
    assert decision["level"] == "required"
    assert decision["env"] == "unresolved"
    assert decision.get("target_unresolved") is True
    assert MANDATED in decision["description"]


def test_topo_discover_dry_run_becomes_confirmation(env_home):
    """bootstrap：本机未登记 + vigil topo-discover --dry-run → 人工确认。

    （真实死锁闭环：错误信息指引"用 topo-discover 登记"，而它自己过去被拒。）
    """
    env_home(local_registered=False)
    decision = ck("vigil topo-discover -H 203.0.113.99 --dry-run")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["require_confirmation"] is True
    assert MANDATED in decision["description"]


def test_unresolved_readonly_required(env_home):
    """未解析目标的只读动作 → 同样 required（kubectl 多集群无参）。"""
    env_home()
    decision = ck("kubectl get pods")
    assert decision is not None
    assert decision["action"] == "approve"
    assert decision["action_name"] == "query"
    assert decision["require_confirmation"] is True
    assert decision["env"] == "unresolved"


def test_unresolved_approved_executes_e2e(env_home, monkeypatch):
    """同意后即执行：人工回调返回 once → approved=True（不再 BLOCKED）。"""
    env_home()
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
            "ssh root@203.0.113.99 'df -h'", "local", approval_callback=_cb)
    finally:
        approval_module.reset_hermes_interactive_context(token)

    assert result["approved"] is True
    assert result.get("user_approved") is True
    assert seen["prompts"] == 1
    assert any(MANDATED in d for d in seen["descriptions"])


def test_matrix_missing_still_denies(env_home):
    """matrix.yaml 缺失 → 仍 deny（fail-closed 保留；唯二 deny 来源之一）。"""
    home = env_home(matrix=False)
    decision = ck("ssh root@203.0.113.99 'df -h'")
    assert decision is not None
    assert decision["action"] == "deny"
    assert decision["matrix_missing"] is True
    # approval 链消费为 BLOCKED
    result = approval_module.check_all_command_guards(
        "ssh root@203.0.113.99 'df -h'", "local")
    assert result["approved"] is False
    assert result.get("ops_matrix_missing") is True
    assert "matrix.yaml" in result["message"]
