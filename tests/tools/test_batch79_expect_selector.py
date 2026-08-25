"""batch79（OPS-DELTA #94）：expect kubectl pod 通道读真实 selector 验收测试。

背景（2026-08-26 用户 node1 实测）：k8s 推荐 label（app.kubernetes.io/name）
应用（argocd 等）没有 `app=` label——batch78 硬编码 `-l app=<name>` 查询返回空
（No resources found）→ 响应体空 → expect 必败，pod 明明 Running 却永远查不到。

修法：pod 通道先读 deployment 真实 selector（.spec.selector.matchLabels）再按
真实 label 查 pod；selector 为空回退 app=<name>；读取失败 fail-closed。
expect.selector 显式覆盖（跳过读取）；expect.label 兼容旧行为。

用 fake kubectl（临时 bin/）真实执行生成的 shell 命令，验证最终 stdout 是
pod 列表、selector 中间产物不进 body。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tools import runbook_handlers as rh
from tools.runbook_exec import execute_runbook

FAKE_KUBECTL = r"""#!/bin/bash
echo "$SCENARIO|$*" >> "$FAKE_KUBECTL_LOG"
case "$SCENARIO" in
  selector-ok)
    if [[ "$*" == *"jsonpath="* ]]; then
      printf '%s' '{"app.kubernetes.io/name":"argocd-redis"}'
      exit 0
    fi
    if [[ "$*" == *"get pods"* ]]; then
      if [[ "$*" == *"app.kubernetes.io/name"* ]]; then
        echo "argocd-redis-7db597db9d-bzz46   1/1   Running   0   2m"
      else
        echo "No resources found in argocd namespace."
      fi
      exit 0
    fi
    ;;
  selector-empty)
    if [[ "$*" == *"jsonpath="* ]]; then
      exit 0
    fi
    if [[ "$*" == *"get pods"* ]]; then
      echo "argocd-redis-7db597db9d-bzz46   1/1   Running   0   2m"
      exit 0
    fi
    ;;
  selector-fail)
    echo 'Error from server (NotFound): deployments.apps "argocd-redis" not found' >&2
    exit 1
    ;;
esac
echo "unhandled: $*" >&2
exit 0
"""


def _k8s_target():
    return {"name": "argocd-redis", "type": "cache", "env": "prod",
            "cluster": "beijing_cluster", "managed_by": "kubectl",
            "host": "39.106.217.32", "namespace": "argocd"}


@pytest.fixture
def fakebin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    log = tmp_path / "kubectl.log"
    return bin_dir, log


def _run_shell(cmd: str, fakebin: Path, log: Path, scenario: str):
    env = {
        **os.environ,
        "PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
        "SCENARIO": scenario,
        "FAKE_KUBECTL_LOG": str(log),
    }
    proc = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True,
                          text=True, timeout=30)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def _pod_expect_cmd(target=None, **expect):
    e = {"target": "kubectl", "kind": "pod", "body_contains": "1/1 Running"}
    e.update(expect)
    return rh.generate_expect_check(e, target or _k8s_target())[0]


# ---------------------------------------------------------------------------
# 组 1：真实 selector（fake kubectl 两步）
# ---------------------------------------------------------------------------

def test_pod_channel_reads_real_selector(fakebin):
    """两步合并命令：先读 deployment selector，最终 stdout 是真实 label 的 pod 列表。"""
    bin_dir, log = fakebin
    spec = _pod_expect_cmd()
    proc, calls = _run_shell(spec["cmd"], bin_dir, log, "selector-ok")
    assert proc.returncode == 0
    # 真实 kubectl 列对齐是多空格——归一化后断言（与 evaluate_expect 同口径）
    assert "1/1 Running" in re.sub(r"\s+", " ", proc.stdout)
    joined = " ".join(calls)
    assert "jsonpath=" in joined and "get deployment/argocd-redis" in joined
    assert "get pods -l" in joined
    assert 'app.kubernetes.io/name' in proc.stdout or "1/1" in proc.stdout
    # selector 读取中间产物（jsonpath 的 JSON）不能出现在最终 body
    assert '{"app.kubernetes.io/name"' not in proc.stdout


def test_old_hardcoded_app_label_would_fail(fakebin):
    """修复前硬编码 -l app=<name> 对推荐 label 应用返回 No resources（对照）。"""
    bin_dir, log = fakebin
    old = "kubectl -n argocd get pods -l app=argocd-redis -o wide"
    proc, _ = _run_shell(old, bin_dir, log, "selector-ok")
    assert "No resources found" in proc.stdout


def test_expect_selector_explicit_skips_read():
    """expect.selector 显式提供 → 直接按声明查 pod，不读 deployment。"""
    spec = _pod_expect_cmd(selector="app.kubernetes.io/name=argocd-redis")
    assert spec["shell"] is False
    assert "app.kubernetes.io/name=argocd-redis" in spec["cmd"]
    assert "jsonpath" not in spec["cmd"]


def test_expect_label_compat():
    """expect.label 兼容旧行为：app=<label>。"""
    spec = _pod_expect_cmd(label="redis")
    assert spec["shell"] is False
    assert "get pods -l app=redis -o wide" in spec["cmd"]


def test_selector_read_failure_fail_closed(fakebin):
    """deployment 不存在（selector 读取失败）→ 命令 exit 非 0 + 明确报错。"""
    bin_dir, log = fakebin
    spec = _pod_expect_cmd()
    proc, _ = _run_shell(spec["cmd"], bin_dir, log, "selector-fail")
    assert proc.returncode != 0
    assert "无法读取 deployment argocd-redis selector" in proc.stderr


def test_selector_empty_falls_back_to_app_label(fakebin):
    """selector 为空 → 回退 app=<name>（能查到 pod 不白查）。"""
    bin_dir, log = fakebin
    spec = _pod_expect_cmd()
    proc, calls = _run_shell(spec["cmd"], bin_dir, log, "selector-empty")
    assert proc.returncode == 0
    assert "1/1 Running" in re.sub(r"\s+", " ", proc.stdout)
    assert any("-l app=argocd-redis" in c for c in calls)


# ---------------------------------------------------------------------------
# 组 2：E2E（execute_runbook，app.kubernetes.io/name label 场景）
# ---------------------------------------------------------------------------

@pytest.fixture
def mhome(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text("""
version: 4
environments:
- name: prod
clusters:
- name: beijing_cluster
  type: kubernetes
  env: prod
  host_groups: []
hosts:
- name: 39.106.217.32
  type: host
  env: prod
  cluster: beijing_cluster
  endpoint: 39.106.217.32
  os: Ubuntu 22.04
  credentials: []
""", encoding="utf-8")
    (home / "services" / "39.106.217.32.yaml").write_text("""
host: 39.106.217.32
services:
- name: argocd-redis
  type: cache
  managed_by: kubectl
  attrs:
    namespace: argocd
  source: manual
""", encoding="utf-8")
    from tools.matrix_data import template_matrix, write_matrix
    m = template_matrix("template1")
    from tools.matrix_data import set_level
    for act in ("scale", "query"):
        set_level(m, "local", act, "execute")
    write_matrix(m, home)
    yield home


def test_e2e_scale_expect_pod_with_recommended_label(mhome, monkeypatch):
    """pod 用 app.kubernetes.io/name label、deployment selector 同 → expect
    body_contains '1/1 Running' 通过（修复前硬编码 app= 必败）。"""
    monkeypatch.setattr("tools.runbook_exec.time.sleep", lambda s: None)
    probes = []

    def runner(spec, target):
        cmd = spec.get("cmd") or ""
        if "jsonpath=" in cmd:  # 真实 selector 两步命令 → 查得到 Running pod
            probes.append(cmd)
            return {"exit_code": 0,
                    "stdout": "argocd-redis-7db597db9d-bzz46 1/1 Running 0 2m",
                    "stderr": ""}
        if "-l app=argocd-redis" in cmd:  # 旧硬编码 app= → 空（对照）
            return {"exit_code": 0,
                    "stdout": "No resources found in argocd namespace.",
                    "stderr": ""}
        return {"exit_code": 0, "stdout": "scaled", "stderr": ""}

    data = {
        "name": "redis-restore", "title": "R", "version": 2, "kind": "incident",
        "env": "local",
        "steps": [
            {"id": "restore", "title": "恢复", "action": "scale",
             "params": {"target": "argocd-redis", "replicas": 1},
             "expect": {"target": "kubectl", "kind": "pod",
                        "body_contains": "1/1 Running"}},
        ],
    }
    res = execute_runbook(data, home=mhome, runner=runner)
    assert res["result"] == "ok"
    step = res["steps"][0]
    assert step["expect"]["ok"] is True
    assert probes, "expect 检查应走真实 selector 两步命令"
    assert all("jsonpath=" in c for c in probes)
