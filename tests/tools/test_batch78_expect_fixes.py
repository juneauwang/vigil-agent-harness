"""batch78（OPS-DELTA #93）：执行器 expect 检查四修验收测试。

背景（2026-08-25 实测，runbook_execute 真实执行 argocd-full-stack-recovery）：
expect 检查连环误判——① kubectl 通道默认查 deployment（READY 列 "1/1 1 1"），
写 "1/1 Running"（pod 语义）必失败；② expect 只跑一次，scale 后 pod 还在
ContainerCreating 就判失败；③ 误判触发 rollback 且审批强度与正常步骤相同，
用户惯性批准 rollout undo（L3 高风险）。

覆盖四组：
  1. 通道语义：kubectl 默认查 pod（label app=）+ kind 区分 + evaluate 语义差异；
  2. 轮询：变更类默认轮询、只读类单次、retry 显式覆盖/关闭、全败报 N 次；
  3. 回滚审批：rollback 步骤 force_confirmation=True + ⚠ 前缀 + scheduled 豁免；
  4. 用例：argocd-full-stack-recovery 的 restore-server 语义（kind=pod + retry，
     ContainerCreating→Running 序列不再误判回滚）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools import runbook_handlers as rh
from tools.runbook_exec import _step_approval, execute_runbook
from tools.registry import tool_error  # noqa: F401  (风格对齐其他工具测试)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mhome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 拓扑 fixture：nginx（docker_compose，回滚审批用）+
    argocd-server（kubectl，expect 轮询用）+ 矩阵 t1（local 全 execute）。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "services").mkdir(parents=True)
    (home / "entities").mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    (home / "topology.yaml").write_text("""
version: 4
environments:
- name: local
clusters:
- name: local
  type: docker
  env: local
  host_groups: []
hosts:
- name: LAPTOP-T2JA2ERE
  type: host
  env: local
  cluster: local
  endpoint: 172.18.120.67
  os: Ubuntu 24.04.3 LTS
  credentials: []
""", encoding="utf-8")
    (home / "services" / "LAPTOP-T2JA2ERE.yaml").write_text("""
host: LAPTOP-T2JA2ERE
services:
- name: nginx
  type: gateway
  managed_by: docker_compose
  source: manual
  detail: entities/nginx.yaml
- name: argocd-server
  type: gateway
  managed_by: kubectl
  attrs:
    namespace: argocd
  source: manual
""", encoding="utf-8")
    (home / "entities" / "nginx.yaml").write_text("""
name: nginx
snapshot:
  by_runtime:
    docker_compose:
      project: docker
      services:
      - name: docker-nginx-1
  common:
    config_dir: /etc/nginx
""", encoding="utf-8")
    from tools.matrix_data import template_matrix, write_matrix
    write_matrix(template_matrix("template1"), home)
    yield home


def _k8s_target():
    return {
        "name": "argocd-server", "type": "gateway", "env": "local",
        "cluster": "local", "managed_by": "kubectl", "host": "LAPTOP-T2JA2ERE",
        "endpoint": "172.18.120.67", "os": "", "container": "",
        "compose_project": "", "compose_service": "", "namespace": "argocd",
        "config_dir": "", "data_dir": "",
    }


def _rb_rollback_trigger(**kw):
    """apply 失败 → 触发 rollback-all 的 runbook（同 test_runbook_exec._rb）。"""
    d = {
        "name": "t", "title": "T", "version": 2, "kind": "maintenance",
        "env": "local", "on_failure": "stop",
        "steps": [
            {"id": "backup", "title": "备份", "action": "backup",
             "params": {"target": "nginx", "dest": "/backup/nginx/config/latest"}},
            {"id": "apply", "title": "应用", "action": "apply_config",
             "params": {"target": "nginx",
                        "changes": [{"key": "http.server_tokens", "value": "off"}]},
             "on_failure": {"rollback": "rollback-main"}},
        ],
        "rollback": [{"name": "rollback-main", "steps": [
            {"id": "rb-restore", "title": "恢复", "action": "rollback",
             "params": {"target": "nginx"}},
        ]}],
    }
    d.update(kw)
    return d


def _fail_apply_runner():
    def runner(spec, target):
        if "sed" in json.dumps(spec):
            return {"exit_code": 1, "stdout": "", "stderr": "sed failed"}
        return {"exit_code": 0, "stdout": "200", "stderr": ""}
    return runner


def _add_approved_markers(data):
    """定时豁免预审标记（与 _check_scheduled_exemption 的哈希口径一致）。"""
    payload = {k: v for k, v in data.items()
               if k not in ("approved_at", "approved_by", "approved_version")}
    data["approved_version"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")
    ).hexdigest()[:16]
    data["approved_at"] = "2026-08-25T00:00:00+08:00"
    data["approved_by"] = "manual"


# ---------------------------------------------------------------------------
# 组 1：kubectl 检查通道语义（默认 pod / kind 区分 / evaluate 差异）
# ---------------------------------------------------------------------------

def test_kubectl_default_kind_is_pod():
    """expect 无 kind + "1/1 Running" → 查 pod（label app=，非 get pod/<name>）。"""
    ch = rh.generate_expect_check(
        {"target": "kubectl", "body_contains": "1/1 Running"}, _k8s_target())
    assert "get pods -l app=argocd-server" in ch[0]["cmd"]
    assert "-o wide" in ch[0]["cmd"]
    assert "-n argocd" in ch[0]["cmd"]


def test_kubectl_deployment_kind():
    ch = rh.generate_expect_check(
        {"target": "kubectl", "kind": "deployment", "body_contains": "1/1"},
        _k8s_target())
    assert "get deployment/argocd-server -o wide" in ch[0]["cmd"]


def test_kubectl_sts_kind():
    ch = rh.generate_expect_check(
        {"target": "kubectl", "kind": "sts", "body_contains": "1/1"}, _k8s_target())
    assert "get sts/argocd-server -o wide" in ch[0]["cmd"]
    ch = rh.generate_expect_check(
        {"target": "kubectl", "kind": "statefulset", "body_contains": "1/1"},
        _k8s_target())
    assert "get sts/argocd-server -o wide" in ch[0]["cmd"]


def test_kubectl_bogus_kind_rejected():
    with pytest.raises(rh.UnsupportedCommand):
        rh.generate_expect_check({"target": "kubectl", "kind": "bogus"}, _k8s_target())


def test_evaluate_pod_vs_deployment_semantics():
    """pod 输出 "1/1 Running" 命中；deployment 输出 "1/1 1 1" 不命中 Running 但命中 1/1。"""
    pod = [{"exit_code": 0,
            "stdout": "argocd-server-86678dcc97-n5cfx 1/1 Running 0 37s"}]
    assert rh.evaluate_expect({"body_contains": "1/1 Running"}, pod)[0] is True
    dep = [{"exit_code": 0, "stdout": "argocd-server 1/1 1 1 22d"}]
    assert rh.evaluate_expect({"body_contains": "1/1 Running"}, dep)[0] is False
    assert rh.evaluate_expect({"body_contains": "1/1"}, dep)[0] is True


# ---------------------------------------------------------------------------
# 组 2：expect 轮询（默认按动作类别 / retry 显式覆盖）
# ---------------------------------------------------------------------------

def _probe_runner(probes, ok_after):
    """probe 调用 ok_after 次前返回 ContainerCreating，之后返回 Running。"""
    def runner(spec, target):
        if "get pods -l app=" in (spec.get("cmd") or ""):
            probes.append(1)
            if len(probes) < ok_after:
                return {"exit_code": 0,
                        "stdout": "argocd-server-x 0/1 ContainerCreating 0 10s",
                        "stderr": ""}
            return {"exit_code": 0,
                    "stdout": "argocd-server-86678dcc97-n5cfx 1/1 Running 0 37s",
                    "stderr": ""}
        return {"exit_code": 0, "stdout": "scaled", "stderr": ""}
    return runner


def _scale_rb(**kw):
    d = {
        "name": "s", "title": "S", "version": 2, "kind": "incident", "env": "local",
        "steps": [
            {"id": "scale", "title": "伸缩", "action": "scale",
             "params": {"target": "argocd-server", "replicas": 1},
             "expect": {"target": "kubectl", "kind": "pod",
                        "body_contains": "1/1 Running"}},
        ],
    }
    d.update(kw)
    return d


def test_change_action_polls_until_ready(mhome, monkeypatch):
    """变更类动作默认轮询：前 3 次 ContainerCreating、第 4 次 Running → 通过。"""
    monkeypatch.setattr("tools.runbook_exec.time.sleep", lambda s: None)
    probes = []
    res = execute_runbook(_scale_rb(), home=mhome, runner=_probe_runner(probes, 4))
    assert res["result"] == "ok"
    step = res["steps"][0]
    assert step["expect"]["ok"] is True and step["expect"]["attempts"] == 4
    assert step["expect"]["retry"] == {"attempts": 12, "interval": 10}


def test_readonly_action_single_attempt(mhome):
    """只读类动作（query）expect 失败 → 单次即败，不轮询。"""
    data = {
        "name": "q", "title": "Q", "version": 2, "kind": "incident", "env": "local",
        "steps": [
            {"id": "chk", "title": "探查", "action": "query",
             "params": {"target": "argocd-server", "pattern": "x"},
             "expect": {"target": "kubectl", "kind": "pod",
                        "body_contains": "1/1 Running"}},
        ],
    }
    res = execute_runbook(data, home=mhome, runner=_probe_runner([], 999))
    assert res["result"] == "failed"
    step = res["steps"][0]
    assert step["expect"]["ok"] is False and step["expect"]["attempts"] == 1


def test_retry_explicit_caps_attempts(mhome):
    """expect.retry {attempts: 2, interval: 0} → 最多 2 次，全败报 2 次。"""
    data = _scale_rb(expect_override=None)
    data["steps"][0]["expect"]["retry"] = {"attempts": 2, "interval": 0}
    res = execute_runbook(data, home=mhome, runner=_probe_runner([], 999))
    assert res["result"] == "failed"
    step = res["steps"][0]
    assert step["expect"]["attempts"] == 2
    assert "2 次尝试均失败" in step["error"]


def test_retry_false_disables_polling(mhome, monkeypatch):
    """expect.retry: false + 变更类动作 → 单次（关闭轮询）。"""
    monkeypatch.setattr("tools.runbook_exec.time.sleep", lambda s: None)
    data = _scale_rb()
    data["steps"][0]["expect"]["retry"] = False
    res = execute_runbook(data, home=mhome, runner=_probe_runner([], 999))
    assert res["result"] == "failed"
    assert res["steps"][0]["expect"]["attempts"] == 1
    assert res["steps"][0]["expect"]["retry"] == {"attempts": 1, "interval": 0}


def test_all_attempts_fail_reports_count(mhome):
    """全部 attempts 失败 → status=failed + error 含"N 次尝试均失败，最后一次"。"""
    data = _scale_rb()
    data["steps"][0]["expect"]["retry"] = {"attempts": 3, "interval": 0}
    res = execute_runbook(data, home=mhome, runner=_probe_runner([], 999))
    assert res["result"] == "failed"
    step = res["steps"][0]
    assert step["expect"]["attempts"] == 3
    assert "3 次尝试均失败" in step["error"]
    assert "最后一次" in step["error"]


# ---------------------------------------------------------------------------
# 组 3：回滚步骤审批强化（force_confirmation + ⚠ 前缀 + scheduled 豁免）
# ---------------------------------------------------------------------------

def test_rollback_step_forces_confirmation(mhome, monkeypatch):
    """rollback 场景步骤 → 审批收到 require_confirmation=True + desc 含 ⚠ 回滚。"""
    captured = []
    monkeypatch.setattr(
        "tools.approval.request_ops_approval",
        lambda command, decision: captured.append((command, decision))
        or {"approved": True})
    res = execute_runbook(_rb_rollback_trigger(), home=mhome, runner=_fail_apply_runner())
    assert res["result"] == "rolled_back"
    assert captured, "回滚步骤必须触发人工审批（force_confirmation）"
    command, decision = captured[0]
    assert decision["require_confirmation"] is True
    assert "⚠ 回滚" in command and "rollback-main" in command


def test_normal_step_approval_not_forced(mhome, monkeypatch):
    """矩阵 approve 档位 + 非 force → require_confirmation=False（不强制）。"""
    captured = []
    monkeypatch.setattr(
        "tools.approval.request_ops_approval",
        lambda command, decision: captured.append((command, decision))
        or {"approved": True})
    from tools.matrix_data import load_matrix, set_level, write_matrix
    m = load_matrix(mhome)
    set_level(m, "local", "backup", "approve")
    write_matrix(m, mhome)
    _step_approval(mhome, "local", "backup", "backup 步骤")
    assert captured and captured[0][1]["require_confirmation"] is False
    assert "⚠ 回滚" not in captured[0][0]


def test_step_approval_force_overrides_execute(mhome, monkeypatch):
    """矩阵 execute 档位：无 force 放行不调审批；force → 强制人工确认。"""
    captured = []
    monkeypatch.setattr(
        "tools.approval.request_ops_approval",
        lambda command, decision: captured.append((command, decision))
        or {"approved": True})
    assert _step_approval(mhome, "local", "rollback", "普通回滚") is None
    assert not captured
    _step_approval(mhome, "local", "rollback", "回滚步骤",
                   force_confirmation=True)
    assert captured and captured[0][1]["require_confirmation"] is True


def test_scheduled_rollback_keeps_exemption(mhome, monkeypatch):
    """scheduled=True → 回滚步骤仍豁免逐次审批（不调 request_ops_approval）。"""
    captured = []
    monkeypatch.setattr(
        "tools.approval.request_ops_approval",
        lambda command, decision: captured.append((command, decision))
        or {"approved": True})
    data = _rb_rollback_trigger()
    _add_approved_markers(data)
    res = execute_runbook(data, home=mhome, runner=_fail_apply_runner(),
                          scheduled=True)
    assert res["result"] == "rolled_back"
    assert not captured, "scheduled 执行不应触发逐次审批（资产审批豁免）"


# ---------------------------------------------------------------------------
# 组 4：用例同步（argocd-full-stack-recovery 的 restore-server 语义）
# ---------------------------------------------------------------------------

def test_argocd_recovery_scale_expect_polls_pod(mhome, monkeypatch):
    """restore-server 语义：kind=pod + retry 12×10，ContainerCreating→Running
    序列 → 等待就绪不再误判（不触发 rollback）。"""
    monkeypatch.setattr("tools.runbook_exec.time.sleep", lambda s: None)
    probes = []
    data = {
        "name": "argocd-recovery", "title": "恢复", "version": 2,
        "kind": "incident", "env": "local",
        "steps": [
            {"id": "restore-server", "title": "恢复 argocd-server",
             "action": "scale",
             "params": {"target": "argocd-server", "replicas": 1},
             "expect": {"target": "kubectl", "kind": "pod",
                        "retry": {"attempts": 12, "interval": 10},
                        "body_contains": "1/1 Running"},
             "on_failure": {"rollback": "rollback-all"}},
        ],
        "rollback": [{"name": "rollback-all", "steps": [
            {"id": "rb-server", "title": "回滚 server", "action": "rollback",
             "params": {"target": "argocd-server"}},
        ]}],
    }
    res = execute_runbook(data, home=mhome, runner=_probe_runner(probes, 4))
    assert res["result"] == "ok" and res["rolled_back"] is False
    step = res["steps"][0]
    assert step["expect"]["ok"] is True and step["expect"]["attempts"] == 4
    assert "rollback" not in [s["id"] for s in res["steps"]]
