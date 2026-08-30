"""batch83 目标解析层（tools/target_resolve.py）——高危变更动作强制目标解析。

覆盖五类场景（任务书验收）：ssh / kubectl / docker / 本机 / 未登记主机；
另含 ansible inventory 主机组。核心断言：resolved 状态带实体 env（拓扑表读，
不借会话 env）；解析失败 → failed 状态（调用方走 deny）。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools.target_resolve import (
    HIGH_RISK_MUTATING_ACTIONS,
    TARGET_FAILED,
    TARGET_RESOLVED,
    command_target_required,
    resolve_required_target,
)

TOPO_YAML = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: dev, isolation: relaxed, role: dev}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443", namespaces: [prod, argocd]}
  - {name: k3s-test, env: test, type: k3s, endpoint: "https://203.0.113.25:6443"}
hosts:
  - {name: node1, type: host, env: prod, cluster: k3s-prod, endpoint: "203.0.113.10", detail: entities/node1.yaml}
  - {name: node2, type: host, env: prod, cluster: k3s-prod, endpoint: "203.0.113.11", detail: entities/node2.yaml}
  - {name: test-host, type: host, env: test, cluster: default, endpoint: "192.168.1.20", detail: entities/test-host.yaml}
  - {name: workstation, type: host, env: dev, endpoint: "192.168.1.50", detail: entities/workstation.yaml}
"""

NODE2_YAML = """\
name: node2
type: host
env: prod
attrs:
  public_ip: 203.0.113.12
  internal_ip: 203.0.113.14
"""

TESTHOST_YAML = """\
name: test-host
type: host
env: test
attrs:
  public_ip: 192.168.1.20
"""

WORKSTATION_YAML = """\
name: workstation
type: host
env: dev
attrs:
  internal_ip: 192.168.1.50
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (home / "entities").mkdir()
    for name, text in (("node2", NODE2_YAML), ("test-host", TESTHOST_YAML),
                       ("workstation", WORKSTATION_YAML)):
        (home / "entities" / f"{name}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# 五类场景：ssh / kubectl / docker / 本机 / 未登记主机
# ---------------------------------------------------------------------------

def test_ssh_registered_host_resolves_entity_env(topo_home):
    target = resolve_required_target("ssh root@node2 'rm -rf /var/log'")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "node2"
    assert target["env"] == "prod"
    assert target["label"] == "node2 (prod)"
    assert "ssh" in target["matched_by"]


def test_ssh_bare_ip_maps_to_host(topo_home):
    target = resolve_required_target("ssh root@203.0.113.12 uptime")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "node2" and target["env"] == "prod"


def test_ssh_unregistered_host_fails(topo_home):
    """未登记主机（临时机器）→ 解析失败（任务 3 走 deny，不再借会话 env）。"""
    target = resolve_required_target("ssh root@203.0.113.99 'rm -rf /var/log'")
    assert target["status"] == TARGET_FAILED
    assert "未在拓扑表登记" in target["reason"]
    assert "topo_query" in target["reason"]


def test_kubectl_unique_cluster_resolves(topo_home):
    """拓扑表只有一个集群时，kubectl 无上下文标记也收敛到该集群。"""
    (topo_home / "topology.yaml").write_text("""\
version: 4
environments:
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: k3s-prod, env: prod, type: k3s, endpoint: "https://203.0.113.15:6443"}
hosts:
  - {name: node1, type: host, env: prod, cluster: k3s-prod, endpoint: "203.0.113.10"}
""", encoding="utf-8")
    target = resolve_required_target("kubectl delete namespace prod")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "k3s-prod" and target["env"] == "prod"


def test_kubectl_multi_cluster_without_context_fails(topo_home):
    """多集群 + 未指定 --context/--namespace → 无法确定目标 → 解析失败（deny）。"""
    target = resolve_required_target("kubectl delete namespace prod")
    assert target["status"] == TARGET_FAILED
    assert "多个集群" in target["reason"]


def test_kubectl_context_selects_cluster(topo_home):
    target = resolve_required_target("kubectl --context k3s-test delete pod x -n test")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "k3s-test" and target["env"] == "test"


def test_kubectl_namespace_selects_cluster(topo_home):
    target = resolve_required_target("kubectl delete deploy x --namespace argocd")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "k3s-prod" and target["env"] == "prod"


def test_kubectl_unknown_context_fails(topo_home):
    """kubectl --context 查无此集群 → 解析失败（deny，不硬凑 k3s-prod）。"""
    target = resolve_required_target("kubectl --context nonexistent-ctx delete pod x")
    assert target["status"] == TARGET_FAILED
    assert "未匹配拓扑集群" in target["reason"]


def test_docker_host_flag_resolves_entity(topo_home):
    target = resolve_required_target("docker -H 203.0.113.11:2375 rm -f web")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "node2" and target["env"] == "prod"


def test_docker_long_host_flag_resolves_entity(topo_home):
    target = resolve_required_target("docker --host tcp://203.0.113.10:2375 ps")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "node1" and target["env"] == "prod"


def test_docker_context_resolves_entity(topo_home):
    target = resolve_required_target("docker context use test-host && docker rm -f x")
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "test-host" and target["env"] == "test"


def test_docker_unknown_host_fails(topo_home):
    target = resolve_required_target("docker -H 203.0.113.99:2375 rm -f web")
    assert target["status"] == TARGET_FAILED
    assert "未在拓扑表登记" in target["reason"]


def test_docker_local_socket_resolves_local_host(topo_home):
    target = resolve_required_target(
        "docker -H unix:///var/run/docker.sock rm -f web",
        local_identities=["workstation"],
    )
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "workstation" and target["env"] == "dev"


def test_local_command_resolves_local_host(topo_home):
    """本机命令（无目标参数）→ 本机 host 实体（拓扑表匹配主机名/IP）。"""
    target = resolve_required_target("npm install -g codex", local_identities=["workstation"])
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "workstation" and target["env"] == "dev"
    assert "本机" in target["matched_by"]


def test_local_command_hostname_variants(topo_home):
    """本机身份多候选（hostname/fqdn/IP）任一命中拓扑即解析。"""
    target = resolve_required_target(
        "rm -f /var/log/old.log",
        local_identities=["some-random-name", "192.168.1.50"],
    )
    assert target["status"] == TARGET_RESOLVED
    assert target["entity"] == "workstation" and target["env"] == "dev"


def test_local_host_not_registered_fails(topo_home):
    """本机未登记拓扑 → 解析失败（无'本机默认 dev'兜底）。"""
    target = resolve_required_target("npm install -g codex", local_identities=["ghost-machine"])
    assert target["status"] == TARGET_FAILED
    assert "本机未在拓扑表登记" in target["reason"]
    assert "topo_query" in target["reason"]


def test_no_topology_fails(topo_home):
    (topo_home / "topology.yaml").unlink()
    target = resolve_required_target("npm install -g codex", local_identities=["workstation"])
    assert target["status"] == TARGET_FAILED
    assert "拓扑表不可用" in target["reason"]


def test_ansible_inventory_group_resolves_env(topo_home):
    inv = topo_home / "hosts.ini"
    inv.write_text("[prod-group]\nnode1\nnode2\n", encoding="utf-8")
    target = resolve_required_target(f"ansible -i {inv} prod-group -m shell -a 'rm -rf /x'")
    assert target["status"] == TARGET_RESOLVED
    assert target["env"] == "prod"
    assert "ansible" in target["matched_by"]


def test_ansible_inventory_mixed_env_fails(topo_home):
    inv = topo_home / "mixed.ini"
    inv.write_text("[mixed]\nnode1\ntest-host\n", encoding="utf-8")
    target = resolve_required_target(f"ansible -i {inv} mixed -m ping")
    assert target["status"] == TARGET_FAILED
    assert "跨环境" in target["reason"]


def test_ansible_inventory_unknown_host_fails(topo_home):
    inv = topo_home / "unknown.ini"
    inv.write_text("[x]\nghost-node\n", encoding="utf-8")
    target = resolve_required_target(f"ansible-playbook -i {inv} site.yml")
    assert target["status"] == TARGET_FAILED
    assert "拓扑表外主机" in target["reason"]


def test_empty_or_non_string_fails():
    assert resolve_required_target("")["status"] == TARGET_FAILED
    assert resolve_required_target(None)["status"] == TARGET_FAILED
    assert resolve_required_target("   ")["status"] == TARGET_FAILED


# ---------------------------------------------------------------------------
# 目标解析判定（任务 3 调用侧）
# ---------------------------------------------------------------------------

def test_high_risk_action_set():
    assert HIGH_RISK_MUTATING_ACTIONS == {
        "install", "remove", "decommission", "reboot", "scale",
    }


def test_command_target_required():
    assert command_target_required({"install"}, "npm install -g codex") is True
    assert command_target_required({"remove"}, "rm -rf /data") is True
    assert command_target_required({"decommission"}, "kubectl delete ns x") is True
    assert command_target_required({"reboot"}, "systemctl reboot") is True
    assert command_target_required({"scale"}, "kubectl scale deploy/x --replicas=2") is True
    # 只读动作 → 不强制解析（天然无目标实体，不误伤）
    assert command_target_required({"query"}, "ls -la") is False
    assert command_target_required({"fetch_log"}, "kubectl logs -f deploy/x") is False
    assert command_target_required({"verify"}, "curl -s /health") is False
    # 低危变更（首批过渡态）→ 暂不强制解析
    assert command_target_required({"restart"}, "systemctl restart nginx") is False
    assert command_target_required({"upgrade"}, "helm upgrade app chart") is False
    # ssh 族受控通道 → 强制解析（不裸连直跑）
    assert command_target_required({"run_script"}, "ssh root@203.0.113.11 'df -h'") is True
    assert command_target_required({"transfer_file"}, "scp a.txt ops@host:/tmp") is True
