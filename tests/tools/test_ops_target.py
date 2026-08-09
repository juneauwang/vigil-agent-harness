"""Command target resolution — 命令目标级 env 判定（ops-agent-harness.md §3 二期）.

从命令字符串解析目标主机 → 拓扑实体 → 目标级 env；解析不到 / 拓扑查无此实体
返回 None（完全走现状）。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools.ops_target import resolve_command_target

TOPO_YAML = """\
version: 1
sources: [test]
environments:
  - name: prod
    entry: "ssh jump@39.106.217.32"
    isolation: strict
    role: prod
    core_entities: [k3s-prod, node1, node2]
  - name: test
    entry: "ssh test-jump"
    isolation: relaxed
    role: test
    core_entities: [test-web]
core_entities:
  - {name: k3s-prod, type: k8s, env: prod, endpoint: "https://10.0.1.100:6443", detail: entities/k3s-prod.yaml}
  - {name: node1, type: k8s-node, env: prod, endpoint: "39.106.217.32", detail: entities/node1.yaml}
  - {name: node2, type: k8s-node, env: prod, detail: entities/node2.yaml}
  - {name: test-web, type: service, env: test, detail: entities/test-web.yaml}
"""

NODE2_YAML = """\
name: node2
type: k8s-node
env: prod
attrs:
  internal_ip: 10.0.1.30
  public_ip: 39.107.92.54
"""

TESTWEB_YAML = """\
name: test-web
type: service
env: test
attrs:
  public_ip: 39.106.200.10
"""


@pytest.fixture
def topo_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "topology.yaml").write_text(TOPO_YAML, encoding="utf-8")
    (home / "entities").mkdir()
    (home / "entities" / "node2.yaml").write_text(NODE2_YAML, encoding="utf-8")
    (home / "entities" / "test-web.yaml").write_text(TESTWEB_YAML, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


def test_ssh_public_ip_maps_to_node2_prod(topo_home):
    target = resolve_command_target("ssh root@39.107.92.54 'rm -rf /var/log'")
    assert target is not None
    assert target["entity"] == "node2"
    assert target["env"] == "prod"
    assert target["label"] == "node2 (prod)"


def test_ssh_internal_ip_maps_to_node2_prod(topo_home):
    target = resolve_command_target("ssh root@10.0.1.30 uptime")
    assert target["entity"] == "node2" and target["env"] == "prod"


def test_ssh_endpoint_ip_maps_to_node1_prod(topo_home):
    target = resolve_command_target("ssh root@39.106.217.32 'df -h'")
    assert target["entity"] == "node1" and target["env"] == "prod"


def test_ssh_bare_ip_prefers_exact_endpoint_over_ported_service(topo_home):
    # harbor 的 endpoint "39.106.217.32:30443" 端口剥离后与 node1 的裸 IP
    # endpoint 同 host——含匹配按列表序会先命中 harbor；全等匹配必须赢。
    home = topo_home
    topo = home / "topology.yaml"
    topo.write_text(
        """version: 1
sources: [test]
environments:
  - name: prod
    entry: "ssh jump@39.106.217.32"
    isolation: strict
    role: prod
    core_entities: [harbor, node1]
core_entities:
  - {name: harbor, type: registry, env: prod, endpoint: "39.106.217.32:30443", detail: entities/harbor.yaml}
  - {name: node1, type: k8s-node, env: prod, endpoint: "39.106.217.32", detail: entities/node1.yaml}
""",
        encoding="utf-8",
    )
    (home / "entities" / "harbor.yaml").write_text(
        'name: harbor\nendpoint: "39.106.217.32:30443"\n', encoding="utf-8"
    )
    (home / "entities" / "node1.yaml").write_text(
        'name: node1\nendpoint: "39.106.217.32"\nattrs:\n  public_ip: 39.106.217.32\n',
        encoding="utf-8",
    )
    target = resolve_command_target("ssh root@39.106.217.32 'df -h'")
    assert target is not None
    assert target["entity"] == "node1" and target["env"] == "prod"


def test_ssh_bare_ip_falls_back_to_ported_service_endpoint(topo_home):
    # 拓扑里没有裸 IP 实体时，包含匹配兜底仍把 IP 归到同 host 的端口服务。
    home = topo_home
    topo = home / "topology.yaml"
    topo.write_text(
        """version: 1
sources: [test]
environments:
  - name: prod
    entry: "ssh jump@39.106.217.32"
    isolation: strict
    role: prod
    core_entities: [harbor]
core_entities:
  - {name: harbor, type: registry, env: prod, endpoint: "39.106.217.32:30443", detail: entities/harbor.yaml}
""",
        encoding="utf-8",
    )
    (home / "entities" / "harbor.yaml").write_text(
        'name: harbor\nendpoint: "39.106.217.32:30443"\n', encoding="utf-8"
    )
    target = resolve_command_target("ssh root@39.106.217.32 'curl -s localhost:5000'")
    assert target is not None
    assert target["entity"] == "harbor" and target["env"] == "prod"


def test_ssh_hostname_maps_by_entity_name(topo_home):
    target = resolve_command_target("ssh root@node2 whoami")
    assert target["entity"] == "node2" and target["env"] == "prod"


def test_ssh_test_web_ip_maps_to_test_env(topo_home):
    target = resolve_command_target("ssh root@39.106.200.10 'rm -rf x'")
    assert target["entity"] == "test-web" and target["env"] == "test"


def test_ssh_test_web_hostname_maps_to_test_env(topo_home):
    target = resolve_command_target("ssh root@test-web uptime")
    assert target["entity"] == "test-web" and target["env"] == "test"


def test_kubectl_associates_k3s_prod(topo_home):
    target = resolve_command_target("kubectl delete namespace prod")
    assert target["entity"] == "k3s-prod" and target["env"] == "prod"


def test_ssh_target_takes_precedence_over_kubectl(topo_home):
    target = resolve_command_target("ssh root@39.107.92.54 'kubectl delete namespace foo'")
    assert target["entity"] == "node2" and target["env"] == "prod"


def test_no_target_returns_none(topo_home):
    assert resolve_command_target("ls -la") is None
    assert resolve_command_target("docker ps") is None
    assert resolve_command_target("df -h /data") is None


def test_unknown_ip_not_in_topo_returns_none(topo_home):
    # 临时机器：拓扑查无此实体 → 走现状，不 fail-closed
    assert resolve_command_target("ssh root@203.0.113.99 'df -h'") is None


def test_non_ssh_user_at_host_ignored_unless_ip(topo_home):
    # git@github.com:org/repo 不该被当成目标
    assert resolve_command_target("git push origin main") is None
    assert resolve_command_target("git remote set-url origin git@github.com:org/repo.git") is None


def test_empty_or_non_string_returns_none(topo_home):
    assert resolve_command_target("") is None
    assert resolve_command_target(None) is None


def test_no_topology_returns_none(tmp_path, monkeypatch):
    home = tmp_path / "empty_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    try:
        assert resolve_command_target("ssh root@39.107.92.54 'rm -rf /var/log'") is None
    finally:
        hc._LOAD_CONFIG_CACHE.clear()
