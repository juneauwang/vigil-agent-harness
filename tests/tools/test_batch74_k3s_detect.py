"""batch74（OPS-DELTA #89）任务 3：K3S runtime 误判修复验收测试。

覆盖（任务书 §任务 3）：
  - kubectl version server gitVersion 含 +k3s → runtime k3s（role worker）；
  - 标准 kubeadm（gitVersion v1.30.x 无 k3s、无 /etc/rancher）→ runtime
    kubernetes（不再一律误标 k3s；role 仍 worker）；
  - kubectl 不可用 → runtime 保持 unknown（落盘形态 ["bare"]），不跑 k3s 探测；
  - /etc/rancher 安装路径存在 → runtime k3s；
  - 两条特征探测都失败（探测不可用）→ 保守标 kubernetes；
  - docker 可用时 docker 优先（k3s 探测不覆盖 docker runtime）。
"""

from __future__ import annotations

from tools.topo_discovery import ProbeResult, discover_host

KUBE = (
    '{"items":['
    '{"kind":"Service","metadata":{"name":"grafana","namespace":"monitoring"},'
    '"spec":{"ports":[{"port":3000,"nodePort":30030}]}},'
    '{"kind":"Deployment","metadata":{"name":"grafana","namespace":"monitoring"},'
    '"spec":{"template":{"spec":{"containers":[{"image":"grafana/grafana:10"}]}}}}'
    ']}'
)

# k3s 的 server gitVersion 形如 v1.30.2+k3s1（grep 后 stdout 只留 k3s1）。
K3S_VERSION = "k3s1\n"
# 标准 kubeadm gitVersion 形如 v1.30.5——grep 'k3s[0-9]*' 无命中 → stdout 空。
STD_VERSION = ""
RANCHER_DIR = "/etc/rancher\n"


class Runner:
    """k8s 特征探测专用 mock runner：最长前缀优先，缺省探测失败（exit 127）。"""

    def __init__(self, **probes):
        self.probes = probes
        self.calls: list = []

    def __call__(self, cmd: str) -> ProbeResult:
        self.calls.append(cmd)
        matches = [(k, v) for k, v in self.probes.items() if k in cmd]
        if matches:
            _, value = max(matches, key=lambda kv: len(kv[0]))
            if isinstance(value, ProbeResult):
                return value
            if isinstance(value, tuple):
                return ProbeResult(value[0], value[1], value[2] if len(value) > 2 else "")
            return ProbeResult(value)
        return ProbeResult("", 127)


def _runner(**overrides):
    probes = {
        "docker ps": ("", 127),           # docker 不可用 → runtime 不落 docker
        "compose ls": ("", 127),
        "kubectl get deploy": KUBE,
        "kubectl version": ("", 127),     # 默认：kubectl version 探测失败
        "/etc/rancher": ("", 2),
        "ss -tlnp": "",
        "nvidia-smi": ("", 127),
    }
    probes.update(overrides)
    return Runner(**probes)


def _discover(overrides=None):
    runner = _runner(**(overrides or {}))
    d = discover_host("203.0.113.20", "prod", runner=runner)
    return d, runner


def test_k3s_gitversion_detects_k3s():
    d, runner = _discover({"kubectl version": K3S_VERSION})
    assert d["host"]["runtime"] == ["k3s"]
    assert d["host"]["role"] == ["worker"]
    assert d["probes"]["k3s"] == "detected"
    assert any("kubectl version" in c for c in runner.calls)


def test_standard_kubeadm_is_kubernetes_not_k3s():
    d, runner = _discover({"kubectl version": STD_VERSION})
    assert d["host"]["runtime"] == ["kubernetes"]
    assert d["host"]["role"] == ["worker"]   # 标准 k8s 节点仍是 worker
    assert d["probes"]["k3s"] == "not-detected(standard-kubernetes)"


def test_kubectl_unavailable_keeps_unknown():
    d, runner = _discover({"kubectl get deploy": ("", 127)})
    assert d["probes"]["kubectl"] != "ok"
    # runtime 保持 unknown → v0.4 落盘形态 ["bare"]。
    assert d["host"]["runtime"] == ["bare"]
    # kubectl 不可用 → 不跑 k3s 特征探测。
    assert not any("kubectl version" in c for c in runner.calls)


def test_etc_rancher_dir_detects_k3s():
    d, runner = _discover({"kubectl version": STD_VERSION, "/etc/rancher": RANCHER_DIR})
    assert d["host"]["runtime"] == ["k3s"]
    assert d["probes"]["k3s"] == "detected"
    assert any("/etc/rancher" in c for c in runner.calls)


def test_both_features_failed_conservative_kubernetes():
    """两条特征探测都失败/超时 → 保守标 kubernetes（不误标 k3s）。"""
    d, _ = _discover()
    assert d["host"]["runtime"] == ["kubernetes"]
    assert d["probes"]["k3s"] == "not-detected(standard-kubernetes)"


def test_docker_present_wins_over_k3s_probe():
    """docker 可用时 runtime=docker（k3s 探测只在 runtime==unknown 时跑）。"""
    d, runner = _discover({
        "docker ps": '{"Names":"web-1","State":"running"}\n',
        "kubectl version": K3S_VERSION,
    })
    assert d["host"]["runtime"] == ["docker"]
    assert d["host"]["role"] == ["docker-host"]
    assert not any("kubectl version" in c for c in runner.calls)
