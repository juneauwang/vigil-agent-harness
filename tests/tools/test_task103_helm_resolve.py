"""task103 任务 1：helm 目标解析分支（tools/target_resolve.py `_resolve_helm`）。

覆盖 helm × {upgrade, install, rollback, uninstall} × {显式 --kube-context、
- 无参单集群兜底、多集群无参 → failed、-n <ns>}。

反证：基线 8ba4b5ce 无 helm 分支 → helm 落 `_resolve_local` → 解析成本机
（workstation/dev），实体名/env 断言全部失败。
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hc
from tools.target_resolve import TARGET_FAILED, TARGET_RESOLVED, resolve_required_target

SINGLE_TOPO = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: heyuan_cluster, env: prod, type: kubernetes, endpoint: "https://47.120.50.110:6443", namespaces: [vllm, monitoring]}
hosts:
  - {name: workstation, type: host, env: dev, endpoint: "192.168.1.50"}
"""

MULTI_TOPO = """\
version: 4
environments:
  - {name: local, isolation: relaxed, role: local}
  - {name: test, isolation: relaxed, role: test}
  - {name: prod, isolation: strict, role: prod}
clusters:
  - {name: heyuan_cluster, env: prod, type: kubernetes, endpoint: "https://47.120.50.110:6443", namespaces: [vllm, monitoring]}
  - {name: beijing_cluster, env: test, type: kubernetes, endpoint: "https://10.0.0.9:6443", namespaces: [staging]}
hosts:
  - {name: workstation, type: host, env: dev, endpoint: "192.168.1.50"}
"""


@pytest.fixture
def activate(tmp_path, monkeypatch):
    def _activate(topo: str = SINGLE_TOPO):
        (tmp_path / "topology.yaml").write_text(topo, encoding="utf-8")
        (tmp_path / "entities").mkdir(exist_ok=True)
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        monkeypatch.setattr(
            "tools.target_resolve._local_host_identities", lambda: ["workstation"]
        )
        hc._LOAD_CONFIG_CACHE.clear()
        return tmp_path

    yield _activate
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.mark.parametrize("verb", ["upgrade", "install", "rollback", "uninstall"])
def test_helm_explicit_kube_context_resolves_cluster(activate, verb):
    activate(MULTI_TOPO)
    cmd = f"helm {verb} vllm-stack ./chart --kube-context heyuan_cluster"
    result = resolve_required_target(cmd)
    assert result["status"] == TARGET_RESOLVED, result
    assert result["entity"] == "heyuan_cluster"
    assert result["env"] == "prod"


def test_helm_upgrade_install_form_resolves_cluster(activate):
    activate(MULTI_TOPO)
    result = resolve_required_target(
        "helm upgrade --install vllm-stack ./chart --kube-context heyuan_cluster -n vllm"
    )
    assert result["status"] == TARGET_RESOLVED, result
    assert result["entity"] == "heyuan_cluster"
    assert result["env"] == "prod"


def test_helm_single_cluster_fallback(activate):
    activate(SINGLE_TOPO)
    result = resolve_required_target("helm upgrade vllm-stack ./chart")
    assert result["status"] == TARGET_RESOLVED, result
    assert result["entity"] == "heyuan_cluster"
    assert result["env"] == "prod"


def test_helm_namespace_single_cluster_fallback(activate):
    activate(SINGLE_TOPO)
    result = resolve_required_target("helm upgrade vllm-stack ./chart -n vllm")
    assert result["status"] == TARGET_RESOLVED, result
    assert result["entity"] == "heyuan_cluster"
    assert result["env"] == "prod"


def test_helm_namespace_matches_cluster_namespaces(activate):
    activate(MULTI_TOPO)
    result = resolve_required_target("helm upgrade vllm-stack ./chart -n staging")
    assert result["status"] == TARGET_RESOLVED, result
    assert result["entity"] == "beijing_cluster"
    assert result["env"] == "test"


def test_helm_multi_cluster_without_arg_fails(activate):
    activate(MULTI_TOPO)
    result = resolve_required_target("helm upgrade vllm-stack ./chart")
    assert result["status"] == TARGET_FAILED, result


def test_helm_unknown_context_fails(activate):
    activate(MULTI_TOPO)
    result = resolve_required_target(
        "helm upgrade vllm-stack ./chart --kube-context nope_cluster"
    )
    assert result["status"] == TARGET_FAILED, result


def test_helm_kubeconfig_single_cluster_fallback(activate):
    activate(SINGLE_TOPO)
    result = resolve_required_target(
        "helm upgrade vllm-stack ./chart --kubeconfig /tmp/kubeconfig"
    )
    assert result["status"] == TARGET_RESOLVED, result
    assert result["entity"] == "heyuan_cluster"
