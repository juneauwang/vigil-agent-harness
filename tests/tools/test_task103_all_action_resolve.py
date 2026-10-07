"""task103 任务 2：目标解析覆盖全部动作（判据 = 命令含目标类 token）。

- 命中（ssh族/kubectl/helm/docker/ansible）→ 按 token 收敛到拓扑实体，实体
  env 查矩阵；
- 不命中（ls/cat/pip install…）→ 本机实体（已登记 = local）；
- 只读动作同样解析（不再"只读不解析"）。

反证：基线 8ba4b5ce 判据是"高危动作/ssh族"，只读命令不解析 → 走会话 env；
且 `command_target_required` 旧签名需 actions 参数。断言全部失败。
"""

from __future__ import annotations

import yaml

import pytest

import hermes_cli.config as hc
from tools.ops_permissions import check_ops_command_permission as ck
from tools.target_resolve import (
    TARGET_RESOLVED,
    command_target_required,
    resolve_required_target,
)

TOPO = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443"}
hosts:
  - {name: workstation, type: host, env: local, endpoint: "192.168.1.50"}
  - {name: node-prod, type: host, env: prod, endpoint: "203.0.113.11"}
"""

# prod 只读 = required（与 test 会话的 execute 区分），用于证明只读也按实体 env。
MATRIX = {
    "test": {"query": "execute", "fetch_log": "execute"},
    "local": {"query": {"approve": "required"}, "install": "execute"},
    "prod": {"query": {"approve": "required"}, "install": {"approve": "required"}},
}


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    (tmp_path / "topology.yaml").write_text(TOPO, encoding="utf-8")
    (tmp_path / "matrix.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "updated_at": "2026-10-07T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": MATRIX,
        "sources": {e: {a: "test" for a in c} for e, c in MATRIX.items()},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "ops:\n  permissions:\n    enabled: true\n    env: test\n    role: test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.target_resolve._local_host_identities", lambda: ["workstation"])
    hc._LOAD_CONFIG_CACHE.clear()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def test_command_target_required_token_judge():
    """判据换代：命令含目标类 token → True；否则 False（与动作无关）。"""
    assert command_target_required("kubectl get pods") is True
    assert command_target_required("helm list") is True
    assert command_target_required("docker ps") is True
    assert command_target_required("ssh root@203.0.113.11 'tail -f x'") is True
    assert command_target_required("ls -la") is False
    assert command_target_required("pip install requests") is False


def test_readonly_kubectl_resolves_entity_env(env_home):
    """只读 `kubectl get` 解析到 k3s-prod(prod) → prod query required（非会话 execute）。"""
    decision = ck("kubectl get pods")
    assert decision is not None
    assert decision["action_name"] == "query"
    assert decision["env"] == "prod"
    assert decision["level"] == "required"


def test_readonly_helm_resolves_entity_env(env_home):
    """只读 `helm list`（规则表外 → unknown）单集群兜底 → k3s-prod(prod)。"""
    decision = ck("helm list")
    assert decision is not None
    assert decision["env"] == "prod"      # 目标实体 env（非会话 test）
    assert decision["action_name"] == "unknown"


def test_readonly_ssh_registered_resolves_entity_env(env_home):
    """已登记主机的 ssh 只读命令 → 目标实体 env=prod。"""
    target = resolve_required_target("ssh root@203.0.113.11 'tail -f /var/log/syslog'")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "node-prod"
    assert target["env"] == "prod"


def test_no_token_readonly_falls_to_local_entity(env_home):
    """无目标 token 的只读命令 → 本机实体（workstation=local）→ local 行 required。"""
    decision = ck("systemctl status nginx")
    assert decision is not None
    assert decision["action_name"] == "query"
    assert decision["env"] == "local"
    assert decision["level"] == "required"


def test_no_token_local_entity_resolution(env_home):
    """ls/cat 无 token → resolve_required_target 落本机 workstation(local)。"""
    for cmd in ("ls -la", "cat /etc/hosts"):
        target = resolve_required_target(cmd)
        assert target["status"] == TARGET_RESOLVED, (cmd, target)
        assert target["entity"] == "workstation", (cmd, target)
        assert target["env"] == "local", (cmd, target)


def test_no_token_pip_install_local_execute(env_home):
    """pip install（无 target token）→ 本机 local 行 install=execute → 直接跑。"""
    assert ck("pip install requests") is None
