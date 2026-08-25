"""batch79（OPS-DELTA #94）：rollback 破坏性修复验收测试。

背景（2026-08-26 实测）：runbook 修复步骤（prepare/scale/set resources）已产生
新 revision 后，rollback 的 `kubectl rollout undo` 回滚到上一个 revision——把
修复冲掉（补好 resources 的 template 被 undo 回滚到空 resources 版本 →
Gatekeeper 拒 → 死循环）。

修法（方案 A+C）：rollback 前先 `rollout status --timeout=5s` 健康检查——当前
健康 → 跳过 undo（记录"当前健康无需回滚"）；不健康才 undo。`force_undo: true`
跳过检查直接 undo（人工确认后）。审批门不削弱（undo 仍走矩阵 + 回滚步骤
force_confirmation）。

用 fake kubectl（临时 bin/）真实执行生成的 shell 命令，验证健康/不健康分支
与 undo 调用序列。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools import runbook_handlers as rh

FAKE_KUBECTL = r"""#!/bin/bash
echo "$SCENARIO|$*" >> "$FAKE_KUBECTL_LOG"
case "$SCENARIO" in
  undo-healthy)
    if [[ "$*" == *"rollout status"* ]]; then
      echo 'deployment "argocd-redis" successfully rolled out'
      exit 0
    fi
    echo 'deployment "argocd-redis" rolled back'
    exit 0
    ;;
  undo-unhealthy)
    if [[ "$*" == *"rollout status"* ]]; then
      echo 'error: deployment "argocd-redis" exceeded its progress deadline' >&2
      exit 1
    fi
    echo 'deployment "argocd-redis" rolled back'
    exit 0
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


def _rollback_spec(**params):
    p = {"target": "argocd-redis"}
    p.update(params)
    return rh.generate_commands("rollback", p, _k8s_target())[0]


def test_rollback_default_has_health_check():
    """默认 rollback = 先 rollout status 健康检查 + 条件 undo（一条 bash 命令）。"""
    spec = _rollback_spec()
    assert spec["shell"] is True
    assert "rollout status deployment/argocd-redis --timeout=5s" in spec["cmd"]
    assert "当前健康无需回滚" in spec["cmd"]
    assert "rollout undo deployment/argocd-redis" in spec["cmd"]


def test_rollback_healthy_skips_undo(fakebin):
    """当前健康（rollout status exit 0）→ 跳过 undo，输出"当前健康无需回滚"。"""
    bin_dir, log = fakebin
    spec = _rollback_spec()
    proc, calls = _run_shell(spec["cmd"], bin_dir, log, "undo-healthy")
    assert proc.returncode == 0
    assert "当前健康无需回滚" in proc.stdout
    assert any("rollout status" in c for c in calls)
    assert not any("rollout undo" in c for c in calls), "健康时不得执行 undo"


def test_rollback_unhealthy_executes_undo(fakebin):
    """当前不健康（rollout status exit 非 0）→ 执行 undo。"""
    bin_dir, log = fakebin
    spec = _rollback_spec()
    proc, calls = _run_shell(spec["cmd"], bin_dir, log, "undo-unhealthy")
    assert proc.returncode == 0
    assert "rolled back" in proc.stdout
    assert any("rollout status" in c for c in calls)
    assert any("rollout undo" in c for c in calls), "不健康时必须执行 undo"


def test_rollback_force_undo_skips_health_check(fakebin):
    """force_undo: true → 跳过健康检查直接 undo（命令无 rollout status）。"""
    bin_dir, log = fakebin
    spec = _rollback_spec(force_undo=True)
    assert spec["shell"] is False
    assert "rollout undo deployment/argocd-redis" in spec["cmd"]
    assert "rollout status" not in spec["cmd"]
    proc, calls = _run_shell(spec["cmd"], bin_dir, log, "undo-unhealthy")
    assert proc.returncode == 0
    assert not any("rollout status" in c for c in calls)
    assert any("rollout undo" in c for c in calls)


def test_rollback_to_revision_kept_in_undo():
    """显式 to（revision）保留在 undo 分支；健康检查仍先行。"""
    spec = _rollback_spec(to="3")
    assert "rollout status deployment/argocd-redis --timeout=5s" in spec["cmd"]
    assert "rollout undo deployment/argocd-redis --to-revision=3" in spec["cmd"]
