"""YAPL P4（批次五十五/五十六）：执行引擎 + 命令生成器验收测试。

覆盖（任务书 §P4 测试清单 阶段 1/2）：
  - 命令生成表：restart×systemd/docker/docker_compose/kubectl/pm2、
    query/fetch_log/verify 多通道、未覆盖组合报错（不猜命令）；
  - 执行引擎（mock 执行通道）：变量替换（跨步骤 / trigger_context / 引用
    不存在报错）、target 解析（多态 / 不存在拒绝）、审批门三态（execute /
    approve / {approve: required} 强制人工）、on_failure 四形态（stop /
    continue / rollback / 场景引用 + rollback 后终止 + rollback 失败强制
    stop）、expect 断言（通过 / 失败 / 通道枚举）；
  - 执行记录（事后审计数据模型）：JSONL 落盘 + 查询。
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

import pytest

from tools import terminal_tool
from tools import runbook_handlers as rh
from tools.runbook_exec import (
    _check_scheduled_exemption,
    execute_runbook,
    ledger_path,
    recent_executions,
    resolve_target,
    substitute_params,
    substitute_text,
)
from tools.runbook_tools import _validate_runbook

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mhome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 拓扑 fixture（nginx docker_compose 服务）+ 矩阵 t1。"""
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


def _nginx_target():
    return {
        "name": "nginx", "type": "gateway", "env": "local", "cluster": "local",
        "managed_by": "docker_compose", "host": "LAPTOP-T2JA2ERE",
        "endpoint": "172.18.120.67", "os": "Ubuntu 24.04.3 LTS",
        "container": "docker-nginx-1", "compose_project": "docker",
        "compose_service": "nginx", "namespace": "",
        "config_dir": "/etc/nginx", "data_dir": "",
    }


def _rb(**kw):
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
            {"id": "verify", "title": "验证", "action": "verify", "params": {"target": "nginx"},
             "expect": {"target": "http", "url": "http://127.0.0.1/healthz",
                        "http_status": 200}},
        ],
        "rollback": [{"name": "rollback-main", "steps": [
            {"id": "rb-restore", "title": "恢复", "action": "restore",
             "params": {"target": "nginx", "from": "{{ steps.backup.params.dest }}"}},
        ]}],
    }
    d.update(kw)
    return d


def _ok_runner(calls=None):
    def runner(spec, target):
        if calls is not None:
            calls.append(spec)
        return {"exit_code": 0, "stdout": "200", "stderr": ""}
    return runner


# ---------------------------------------------------------------------------
# 命令生成表（阶段 2）
# ---------------------------------------------------------------------------

class TestCommandGeneration:
    def test_restart_systemd(self):
        t = {**_nginx_target(), "managed_by": "systemd"}
        specs = rh.generate_commands("restart", {"target": "nginx"}, t)
        assert specs[0]["cmd"] == "systemctl restart nginx"
        assert specs[0]["sudo"] is True

    def test_restart_docker(self):
        t = {**_nginx_target(), "managed_by": "docker"}
        specs = rh.generate_commands("restart", {"target": "nginx"}, t)
        assert specs[0]["cmd"] == "docker restart docker-nginx-1"
        assert specs[0]["sudo"] is False

    def test_restart_docker_compose(self):
        specs = rh.generate_commands("restart", {"target": "nginx"}, _nginx_target())
        assert specs[0]["cmd"] == "docker compose -p docker restart nginx"

    def test_restart_kubectl(self):
        t = {**_nginx_target(), "managed_by": "kubectl", "namespace": "prod"}
        specs = rh.generate_commands("restart", {"target": "nginx"}, t)
        assert specs[0]["cmd"] == "kubectl -n prod rollout restart deployment/nginx"

    def test_restart_pm2(self):
        t = {**_nginx_target(), "managed_by": "pm2"}
        specs = rh.generate_commands("restart", {"target": "nginx"}, t)
        assert specs[0]["cmd"] == "pm2 restart nginx"

    def test_enable_disable_kubernetes_scale(self):
        t = {**_nginx_target(), "managed_by": "kubectl"}
        on = rh.generate_commands("enable", {"target": "nginx"}, t)[0]["cmd"]
        off = rh.generate_commands("disable", {"target": "nginx"}, t)[0]["cmd"]
        assert on == "kubectl scale deployment/nginx --replicas=1"
        assert off == "kubectl scale deployment/nginx --replicas=0"

    def test_reboot_shutdown_host(self):
        t = {**_nginx_target(), "type": "host", "managed_by": ""}
        assert rh.generate_commands("reboot", {"target": "h"}, t)[0]["cmd"] == "systemctl reboot"
        assert rh.generate_commands("shutdown", {"target": "h"}, t)[0]["cmd"] == "systemctl poweroff"

    def test_query_multichannel(self):
        t = {**_nginx_target(), "managed_by": "systemd"}
        assert "systemctl status" in rh.generate_commands("query", {"target": "nginx"}, t)[0]["cmd"]
        t = {**_nginx_target(), "managed_by": "kubectl", "namespace": "ops"}
        q = rh.generate_commands("query", {"target": "nginx", "pattern": "nginx"}, t)[0]
        assert q["cmd"].startswith("kubectl -n ops get pods")
        assert q["shell"] is True

    def test_fetch_log_multichannel(self):
        t = {**_nginx_target(), "managed_by": "docker"}
        cmd = rh.generate_commands("fetch_log", {"target": "nginx", "lines": 50, "grep": "ERR"}, t)[0]
        assert "docker logs --tail 50 docker-nginx-1" in cmd["cmd"]
        assert "grep ERR" in cmd["cmd"]
        t = {**_nginx_target(), "managed_by": "systemd"}
        cmd = rh.generate_commands("fetch_log", {"target": "nginx", "lines": 20}, t)[0]
        assert cmd["cmd"] == "journalctl -u nginx -n 20 --no-pager"

    def test_verify_multichannel(self):
        t = {**_nginx_target(), "managed_by": "docker"}
        assert "docker inspect" in rh.generate_commands("verify", {"target": "nginx"}, t)[0]["cmd"]
        t = {**_nginx_target(), "managed_by": "systemd"}
        assert rh.generate_commands("verify", {"target": "nginx"}, t)[0]["cmd"] == "systemctl is-active nginx"

    def test_apply_config_nginx_sed(self):
        specs = rh.generate_commands("apply_config", {
            "target": "nginx",
            "changes": [{"key": "http.server_tokens", "value": "off"}],
        }, _nginx_target())
        assert "sed -i -E" in specs[0]["cmd"]
        assert "docker exec docker-nginx-1" in specs[0]["cmd"]

    def test_apply_config_kubectl_configmap(self):
        t = {**_nginx_target(), "managed_by": "kubectl", "namespace": "ops"}
        specs = rh.generate_commands("apply_config", {
            "target": "nginx",
            "changes": [{"key": "server_tokens", "value": "off"}],
        }, t)
        assert specs[0]["argv"][:4] == ["kubectl", "-n", "ops", "patch"]
        assert "configmap" in specs[0]["argv"]

    def test_backup_restore_container(self):
        t = {**_nginx_target(), "managed_by": "docker"}
        b = rh.generate_commands("backup", {"target": "nginx", "dest": "/b/latest"}, t)[0]
        assert b["cmd"] == "mkdir -p /b && docker cp docker-nginx-1:/etc/nginx /b/latest"
        assert b["shell"] is True
        r = rh.generate_commands("restore", {"target": "nginx", "from": "/b/latest"}, t)[0]
        assert r["cmd"] == "docker cp /b/latest/. docker-nginx-1:/etc/nginx/"
        # 无目录层级 dest → 不包 mkdir，保持纯 argv 形态。
        b2 = rh.generate_commands("backup", {"target": "nginx", "dest": "latest"}, t)[0]
        assert b2["cmd"] == "docker cp docker-nginx-1:/etc/nginx latest"
        assert b2["shell"] is False

    def test_package_os_dispatch(self):
        t = {**_nginx_target(), "type": "host", "managed_by": "", "os": "Ubuntu 24.04"}
        cmd = rh.generate_commands("install", {"target": "h", "package": "nginx"}, t)[0]
        assert cmd["cmd"] == "apt-get install -y nginx" and cmd["sudo"] is True
        t2 = {**t, "os": "Rocky 9.x"}
        cmd2 = rh.generate_commands("upgrade", {"target": "h", "package": "nginx",
                                                "version": "1.24"}, t2)[0]
        assert cmd2["cmd"] == "dnf upgrade -y nginx-1.24"
        rem = rh.generate_commands("remove", {"target": "h", "package": "nginx",
                                              "deps": False}, t)[0]
        assert rem["cmd"] == "apt-get remove -y nginx"

    def test_transfer_and_script_specs(self):
        specs = rh.generate_commands("transfer_file", {
            "source": {"path": "/a"}, "dest": {"host": "h", "path": "/b"},
        }, {})
        assert "transfer" in specs[0]
        specs = rh.generate_commands("run_script", {"script": "backup", "args": ["x"]}, {})
        assert specs[0]["script_asset"] == "backup"

    def test_unsupported_combo_raises(self):
        t = {**_nginx_target(), "managed_by": "bare"}
        with pytest.raises(rh.UnsupportedCommand):
            rh.generate_commands("deploy", {"target": "nginx"}, t)
        with pytest.raises(rh.UnsupportedCommand):
            rh.generate_commands("scale", {"target": "nginx", "replicas": 2}, t)
        with pytest.raises(rh.UnsupportedCommand):
            rh.generate_commands("fetch_log", {"target": "nginx"}, t)

    def test_expect_channels(self):
        t = _nginx_target()
        ch = rh.generate_expect_check({"target": "http", "url": "http://x/healthz",
                                       "http_status": 200}, t)
        assert "curl" in ch[0]["cmd"] and "%{http_code}" in ch[0]["cmd"]
        ch = rh.generate_expect_check({"target": "docker", "object": "c1"}, t)
        assert "docker inspect" in ch[0]["cmd"]
        with pytest.raises(rh.UnsupportedCommand):
            rh.generate_expect_check({"target": "nope"}, t)

    def test_expect_evaluate(self):
        assert rh.evaluate_expect({"http_status": 200},
                                  [{"exit_code": 0, "stdout": "200"}])[0] is True
        ok, detail = rh.evaluate_expect({"http_status": 200},
                                        [{"exit_code": 0, "stdout": "503"}])
        assert ok is False and "503" in detail
        ok, detail = rh.evaluate_expect({"body_contains": "healthy"},
                                        [{"exit_code": 0, "stdout": "{\"status\":\"healthy\"}"}])
        assert ok is True
        ok, detail = rh.evaluate_expect({"contains": {"gw": "running"}},
                                        [{"exit_code": 0, "stdout": "gw running"}])
        assert ok is True


# ---------------------------------------------------------------------------
# 执行引擎（阶段 1）
# ---------------------------------------------------------------------------

class TestExecutionEngine:
    def test_full_run_ok(self, mhome):
        calls = []
        res = execute_runbook(_rb(), home=mhome, runner=_ok_runner(calls))
        assert res["result"] == "ok"
        assert [s["id"] for s in res["steps"]] == ["backup", "apply", "verify"]
        assert all(s["status"] == "ok" for s in res["steps"])
        assert "docker cp docker-nginx-1:/etc/nginx" in calls[0]["cmd"]

    def test_v0_1_rejected(self, mhome):
        v1 = {"name": "v1", "title": "V1", "version": 1, "kind": "incident",
              "steps": [{"id": "s1", "title": "s", "commands": ["echo hi"]}]}
        res = execute_runbook(v1, home=mhome, runner=_ok_runner())
        assert res["result"] == "error" and "v0.1" in res["error"]

    def test_variable_substitution_cross_step(self, mhome, monkeypatch):
        data = _rb()
        data["rollback"][0]["steps"][0]["params"]["from"] = "{{ steps.backup.params.dest }}"
        def bad_apply(spec, target):
            if "sed" in json.dumps(spec):
                return {"exit_code": 1, "stdout": "", "stderr": "sed failed"}
            return {"exit_code": 0, "stdout": "200", "stderr": ""}
        # batch78（OPS-DELTA #93）：回滚步骤强制人工确认——测试走审批回调放行。
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(
            lambda command, description, **k: "once")
        try:
            res = execute_runbook(data, home=mhome, runner=bad_apply)
        finally:
            terminal_tool.set_approval_callback(None)
        rb_step = res["steps"][-1]["steps"][0]
        assert res["result"] == "rolled_back"
        assert rb_step["params"]["from"] == "/backup/nginx/config/latest"

    def test_variable_trigger_context(self, mhome):
        data = {
            "name": "tc", "title": "TC", "version": 2, "kind": "incident",
            "env": "local",
            "steps": [
                {"id": "q", "title": "q", "action": "query",
                 "params": {"target": "nginx", "pattern": "{{ trigger_context.alertname }}"}},
            ],
        }
        res = execute_runbook(data, home=mhome, runner=_ok_runner(),
                              trigger_context={"alertname": "HarborDown"})
        assert res["result"] == "ok"
        assert res["steps"][0]["params"]["pattern"] == "HarborDown"

    def test_variable_missing_ref_stops(self, mhome):
        data = _rb()
        data["rollback"][0]["steps"][0]["params"]["from"] = "{{ steps.nope.params.x }}"
        res = execute_runbook(data, home=mhome, runner=_ok_runner())
        assert res["result"] == "blocked"  # 前置校验（关系层·变量引用）先拦截
        assert "不存在" in res["error"] or "校验失败" in res["error"]

    def test_trigger_context_missing_stops(self, mhome):
        data = {
            "name": "tc2", "title": "TC2", "version": 2, "kind": "incident",
            "env": "local",
            "steps": [{"id": "q", "title": "q", "action": "query",
                       "params": {"pattern": "{{ trigger_context.nope }}"}}],
        }
        res = execute_runbook(data, home=mhome, runner=_ok_runner())
        assert res["result"] == "blocked"  # 前置校验（字段不在触发上下文枚举）先拦截
        assert "校验失败" in res["error"]

    def test_target_not_found_rejected(self, mhome):
        data = {
            "name": "tnf", "title": "TNF", "version": 2, "kind": "incident",
            "env": "local",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "ghost"}}],
        }
        res = execute_runbook(data, home=mhome, runner=_ok_runner())
        assert res["result"] == "blocked"  # 前置校验（引用层·拓扑实体）先拦截
        assert "不在拓扑表" in res["error"]

    def test_target_resolution_polymorphic(self, mhome):
        from tools.topo_tools import load_topology
        topo = load_topology(mhome)
        svc = resolve_target(mhome, topo, "nginx")
        assert svc["type"] == "service" and svc["managed_by"] == "docker_compose"
        assert svc["host"] == "LAPTOP-T2JA2ERE" and svc["container"] == "docker-nginx-1"
        host = resolve_target(mhome, topo, "LAPTOP-T2JA2ERE")
        assert host["type"] == "host" and host["os"].startswith("Ubuntu")
        cluster = resolve_target(mhome, topo, "local")
        assert cluster["type"] == "cluster"

    def test_local_endpoint_matches_interface_ip(self, mhome):
        """本机服务 endpoint 是网卡 IP（非 hostname）时判定 local，不误走 SSH。"""
        from tools.runbook_exec import _is_local_endpoint
        from tools.topo_tools import load_topology
        topo = load_topology(mhome)
        svc = resolve_target(mhome, topo, "nginx")
        assert svc.get("remote") is False
        # 拓扑 endpoint = 172.18.120.67（本机 eth0，非 DNS 名）→ 必须判 local。
        assert _is_local_endpoint(svc.get("endpoint"), svc.get("host")) is True
        # endpoint 带端口（本机服务行 LAPTOP-T2JA2ERE:5003 形态）→ 剥端口判
        # local，不误走 SSH 回环（OPS-DELTA #83 实测修复）。
        assert _is_local_endpoint("LAPTOP-T2JA2ERE:5003", "LAPTOP-T2JA2ERE") is True
        assert _is_local_endpoint("172.18.120.67:5003", svc.get("host")) is True
        # 非本机地址 → remote。
        assert _is_local_endpoint("10.203.0.9", svc.get("host")) is False
        assert _is_local_endpoint("10.203.0.9:5003", svc.get("host")) is False

    def test_remote_host_with_own_ip_endpoint_not_local(self, mhome):
        """远端主机 endpoint == 自身 IP（host_name 同值）必须判 remote——不能因
        endpoint==host_name 误判本地（2026-08-25 实测：阿里云 203.0.113.32
        endpoint==host_name，旧代码 `e == host_name → local` 导致 kubectl 命令
        在本机执行，runbook 执行器全部走错机器 "timed out waiting for the
        condition"）。本机身份只认 hostname/网卡 IP，不认拓扑 host_name。"""
        from tools.runbook_exec import _is_local_endpoint
        # 远端公网 IP 作 endpoint 且 host_name 同名 → 不是本机身份 → remote
        assert _is_local_endpoint("203.0.113.32", "203.0.113.32") is False
        assert _is_local_endpoint("203.0.113.44", "203.0.113.44") is False
        # 带端口形态同样判 remote
        assert _is_local_endpoint("203.0.113.32:22", "203.0.113.32") is False
        # 本机身份（hostname/网卡 IP/localhost）不受影响，仍判 local
        import socket
        assert _is_local_endpoint(socket.gethostname(), "anything") is True
        assert _is_local_endpoint("127.0.0.1", "anything") is True

    def test_approval_execute_level_passes(self, mhome):
        res = execute_runbook(_rb(), home=mhome, runner=_ok_runner())
        assert res["result"] == "ok"  # t1 矩阵 local 全 execute

    def test_approval_required_human_deny(self, mhome, monkeypatch):
        from tools.matrix_data import load_matrix, set_level, write_matrix
        m = load_matrix(mhome)
        set_level(m, "local", "backup", "required")
        write_matrix(m, mhome)
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        denied = []
        terminal_tool.set_approval_callback(
            lambda command, description, **k: denied.append(command) or "deny")
        try:
            res = execute_runbook(_rb(), home=mhome, runner=_ok_runner())
        finally:
            terminal_tool.set_approval_callback(None)
        assert res["steps"][0]["status"] == "blocked"
        assert "BLOCKED" in res["steps"][0]["error"] or "denied" in res["steps"][0]["error"]
        assert denied, "审批回调应被调用（required 强制人工）"

    def test_on_failure_stop(self, mhome):
        def bad_apply(spec, target):
            if "sed" in json.dumps(spec):
                return {"exit_code": 1, "stdout": "", "stderr": "sed failed"}
            return {"exit_code": 0, "stdout": "200", "stderr": ""}
        data = _rb()
        data["steps"][1]["on_failure"] = "stop"
        res = execute_runbook(data, home=mhome, runner=bad_apply)
        assert res["result"] == "failed" and res["rolled_back"] is False
        assert res["steps"][-1]["id"] == "apply"

    def test_on_failure_continue(self, mhome):
        data = {
            "name": "c", "title": "C", "version": 2, "kind": "incident", "env": "local",
            "steps": [
                {"id": "q1", "title": "q1", "action": "query",
                 "params": {"pattern": "x"}, "on_failure": "continue"},
                {"id": "q2", "title": "q2", "action": "query", "params": {"pattern": "y"}},
            ],
        }
        res = execute_runbook(data, home=mhome,
                              runner=lambda spec, target: {"exit_code": 1, "stdout": "",
                                                           "stderr": "no match"})
        assert res["result"] == "failed"
        assert [s["status"] for s in res["steps"]] == ["failed", "failed"]

    def test_on_failure_rollback_terminates(self, mhome, monkeypatch):
        calls = []
        def bad_apply(spec, target):
            calls.append(spec)
            if "sed" in json.dumps(spec):
                return {"exit_code": 1, "stdout": "", "stderr": "sed failed"}
            return {"exit_code": 0, "stdout": "200", "stderr": ""}
        # batch78（OPS-DELTA #93）：回滚步骤强制人工确认——测试走审批回调放行。
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        from tools import terminal_tool
        terminal_tool.set_approval_callback(
            lambda command, description, **k: "once")
        try:
            res = execute_runbook(_rb(), home=mhome, runner=bad_apply)
        finally:
            terminal_tool.set_approval_callback(None)
        assert res["result"] == "rolled_back" and res["rolled_back"] is True
        rb_block = res["steps"][-1]
        assert rb_block["id"] == "__rollback__" and rb_block["steps"][0]["id"] == "rb-restore"
        # rollback 后终止：verify 步骤不执行
        assert "verify" not in [s["id"] for s in res["steps"]]

    def test_on_failure_scene_missing(self, mhome):
        data = _rb()
        data["steps"][1]["on_failure"] = {"rollback": "ghost"}
        res = execute_runbook(data, home=mhome, runner=lambda spec, target: {
            "exit_code": 1 if "sed" in json.dumps(spec) else 0, "stdout": "200", "stderr": ""})
        assert res["result"] == "blocked"  # 前置校验（场景引用）先拦截
        assert "ghost" in res["error"]

    def test_rollback_failure_force_stop(self, mhome):
        def bad_all(spec, target):
            s = json.dumps(spec)
            # restore 命令 = docker cp <本地备份> <容器>（含 /backup/ 源路径）
            if "sed" in s or "docker cp /backup" in s:
                return {"exit_code": 1, "stdout": "", "stderr": "failed"}
            return {"exit_code": 0, "stdout": "200", "stderr": ""}
        res = execute_runbook(_rb(), home=mhome, runner=bad_all)
        assert res["result"] == "failed"
        assert "rollback 失败" in res["error"]

    def test_expect_pass_and_fail(self, mhome):
        res = execute_runbook(_rb(), home=mhome, runner=_ok_runner())
        assert res["steps"][-1]["expect"]["ok"] is True
        def bad_http(spec, target):
            if "curl" in json.dumps(spec):
                return {"exit_code": 0, "stdout": "503", "stderr": ""}
            return {"exit_code": 0, "stdout": "200", "stderr": ""}
        res2 = execute_runbook(_rb(), home=mhome, runner=bad_http)
        assert res2["result"] == "failed"
        assert "http_status" in res2["steps"][-1]["expect"]["detail"]

    def test_ledger_recorded(self, mhome):
        execute_runbook(_rb(), home=mhome, runner=_ok_runner())
        rows = recent_executions(mhome)
        assert len(rows) == 1 and rows[0]["result"] == "ok"
        assert rows[0]["runbook"] == "t" and rows[0]["source"] == "user"
        assert ledger_path(mhome).is_file()
        # 凭据脱敏：输出不含密码（构造一个含 password 的输出也不落盘）
        execute_runbook(_rb(), home=mhome, runner=lambda spec, target: {
            "exit_code": 0, "stdout": "password=supersecret", "stderr": ""})
        rows = recent_executions(mhome, limit=2)
        joined = json.dumps(rows, ensure_ascii=False)
        assert "supersecret" not in joined


# ---------------------------------------------------------------------------
# 定时豁免（阶段 4 前置逻辑，引擎内实现）
# ---------------------------------------------------------------------------

class TestScheduledExemption:
    def _preapproved(self, data):
        data = json.loads(json.dumps(data))
        data["approved_at"] = "2026-08-23T00:00:00+08:00"
        data["approved_by"] = "wpwang"
        payload = {k: v for k, v in data.items()
                   if k not in ("approved_at", "approved_by", "approved_version")}
        data["approved_version"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       default=str).encode()).hexdigest()[:16]
        return data

    def test_unapproved_blocked(self, mhome):
        res = execute_runbook(_rb(), home=mhome, runner=_ok_runner(), scheduled=True)
        assert res["result"] == "blocked"
        assert "未过资产审批" in res["error"]

    def test_preapproved_runs(self, mhome):
        data = self._preapproved(_rb())
        res = execute_runbook(data, home=mhome, runner=_ok_runner(), scheduled=True)
        assert res["result"] == "ok"
        assert res["steps"][0]["status"] == "ok"

    def test_hash_drift_blocked(self, mhome):
        data = self._preapproved(_rb())
        data["title"] = "T2"
        res = execute_runbook(data, home=mhome, runner=_ok_runner(), scheduled=True)
        assert res["result"] == "blocked"
        assert "豁免失效" in res["error"]

    def test_check_exemption_direct(self, mhome):
        assert _check_scheduled_exemption(_rb()) is not None
        assert _check_scheduled_exemption(self._preapproved(_rb())) is None


class TestPipelineShellRegression:
    """批八十一：shell:False + 管道/操作符 隐患回归（ps aux | grep → garbage option）。"""

    def test_query_has_target_no_pattern_shell_true(self):
        from tools.runbook_handlers import generate_commands
        t = {**_nginx_target(), "managed_by": ""}
        specs = generate_commands("query", {"target": "nginx"}, t)
        assert len(specs) == 1
        spec = specs[0]
        assert "ps aux | grep nginx" in spec["cmd"]
        assert spec["shell"] is True  # 管道 + || 必须 bash -c，不再 shlex.split

    def test_query_has_target_no_pattern_runs_via_bash(self, mhome):
        # 真实执行路径：shlex.split 拆碎管道曾报 garbage option，shell=True 后正常
        from tools.runbook_exec import _exec_local, _run_spec
        from tools.runbook_handlers import generate_commands
        t = {**_nginx_target(), "managed_by": "", "remote": False}
        spec = generate_commands("query", {"target": "nginx"}, t)[0]
        res = _run_spec(mhome, t, spec)
        assert res.get("exit_code") == 0, res.get("stderr")

    def test_expect_process_and_port_shell_true(self):
        from tools.runbook_handlers import generate_expect_check
        t = {**_nginx_target(), "managed_by": ""}
        p = generate_expect_check({"target": "process", "pattern": "nginx"}, t)[0]
        assert p["shell"] is True
        port = generate_expect_check({"target": "port", "port": "80"}, t)[0]
        assert port["shell"] is True
        assert "|| exit 1" in port["cmd"]

    def test_no_remaining_cmd_pipeline_with_shell_false(self):
        import ast
        import re
        from pathlib import Path
        src = Path("tools/runbook_handlers.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        hazards = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                pairs = {}
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        pairs[k.value] = v
                cmd = pairs.get("cmd")
                sh = pairs.get("shell")
                if (isinstance(cmd, ast.Constant) and isinstance(cmd.value, str)
                        and isinstance(sh, ast.Constant) and sh.value is False
                        and re.search(r"\|\||&&|[;>]|\|", cmd.value)):
                    hazards.append(cmd.value[:80])
        assert hazards == [], f"shell:False 命令含操作符: {hazards}"


class TestExecutionLock:
    """批八十一：执行级并发锁集成（引擎入口）——同名/同目标冲突拒绝 + 释放。"""

    @pytest.fixture(autouse=True)
    def _clean_locks(self):
        from tools.runbook_lock import _lock as _rl_lock
        from tools.runbook_lock import _registry as _rl_registry
        with _rl_lock:
            _rl_registry.clear()
        yield
        with _rl_lock:
            _rl_registry.clear()

    def test_same_runbook_conflict_blocked(self, mhome):
        import threading
        import time
        from tools.runbook_exec import execute_runbook
        from tools.runbook_lock import list_locks, release

        release("exec_lock_a")
        events: list = []

        def slow_runner(spec, target):
            time.sleep(0.8)
            return {"exit_code": 0, "stdout": "200", "stderr": ""}

        out: dict = {}

        def run_a():
            out["a"] = execute_runbook(
                _rb(), home=mhome, runner=slow_runner,
                exec_id="exec_lock_a", progress_callback=events.append)

        t = threading.Thread(target=run_a)
        t.start()
        time.sleep(0.2)
        out["b"] = execute_runbook(_rb(), home=mhome, runner=_ok_runner(),
                                   exec_id="exec_lock_b",
                                   progress_callback=events.append)
        t.join(timeout=5)
        assert out["b"]["result"] == "blocked"
        assert "锁定中" in out["b"]["error"]
        assert out["a"]["result"] == "ok"
        assert list_locks() == []  # 结束后释放
        done_b = [e for e in events
                  if e.get("type") == "runbook_done" and e.get("exec_id") == "exec_lock_b"]
        assert done_b and done_b[0]["status"] == "blocked"  # 冲突方 SSE 收 blocked 终态

    def test_target_overlap_conflict(self, mhome):
        from tools.runbook_exec import execute_runbook, _runbook_targets
        from tools.runbook_lock import release

        release("exec_lock_t1")
        rb_a = _rb()
        rb_b = _rb(name="other")
        assert _runbook_targets(rb_a) and _runbook_targets(rb_b)
        # rb_a 与 rb_b 都含 target nginx → 重叠
        import threading, time

        def slow_runner(spec, target):
            time.sleep(0.6)
            return {"exit_code": 0, "stdout": "200", "stderr": ""}

        out = {}

        def run_a():
            out["a"] = execute_runbook(rb_a, home=mhome, runner=slow_runner,
                                       exec_id="exec_lock_t1")

        t = threading.Thread(target=run_a)
        t.start()
        time.sleep(0.15)
        out["b"] = execute_runbook(rb_b, home=mhome, runner=_ok_runner(),
                                   exec_id="exec_lock_t2")
        t.join(timeout=5)
        assert out["b"]["result"] == "blocked"
        assert "目标与进行中执行重叠" in out["b"]["error"]
        assert out["a"]["result"] == "ok"
        release("exec_lock_t1")
        release("exec_lock_t2")


class TestMatrixMissingRefusal:
    """batch83-fix（OPS-DELTA #99）验收 e：runbook 执行路径矩阵缺失 → 报错/拒绝
    （与 terminal 一致——矩阵缺失 = 权限系统不可用，fail-closed，不再按空矩阵
    approve 门控放行）。"""

    def test_step_approval_refuses_when_matrix_missing(self, mhome):
        from tools.runbook_exec import _step_approval
        # mhome fixture 铺了矩阵 t1——删掉它模拟"ops 启用但没跑 setup 铺矩阵"。
        (mhome / "matrix.yaml").unlink()
        err = _step_approval(mhome, "test", "restart", "重启 nginx")
        assert err is not None
        assert "matrix.yaml" in err
        assert "vigil setup" in err

    def test_step_approval_disabled_gate_skips_matrix_requirement(self, mhome):
        from tools.runbook_exec import _step_approval
        import hermes_cli.config as hc
        (mhome / "config.yaml").write_text(
            "ops:\n  permissions:\n    enabled: false\n"
            "approvals:\n  mode: off\n",
            encoding="utf-8",
        )
        hc._LOAD_CONFIG_CACHE.clear()
        (mhome / "matrix.yaml").unlink()
        err = _step_approval(mhome, "test", "restart", "重启 nginx")
        # 权限系统显式关闭 → 矩阵缺失不触发"矩阵未初始化"拒绝（与 terminal 一致），
        # 退回空矩阵 approve 门（无人在场仍 fail-closed BLOCK——这是审批门语义，
        # 不是矩阵缺失语义，错误信息不含矩阵未初始化指引）。
        assert err is None or "矩阵未初始化" not in err
        hc._LOAD_CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# task16 M1：步骤 target 实体 env 范围一致性 + 矩阵门跟随实体真实 env
# ---------------------------------------------------------------------------

class TestTargetEnvScope:
    """task16 M1（OPS-DELTA 审查 task12 #M1）：

    (a) env:local runbook target 唯一名 prod 实体 → 步骤拒绝（修复前按 local
        档 execute 自动放行——无人参与改 prod 的绕过路径）；
    (b) 双环境同名实体仍经 scope 收敛正确解析（topo_ref 歧义收敛无回归）；
    (c) runbook env 与实体 env 一致 → 行为与修复前完全一致；
    (d) 子 runbook env 范围链（父 local → 子继承 local）同样拦截 prod target。
    """

    @pytest.fixture
    def ehome(self, tmp_path, monkeypatch):
        """local + prod 双环境拓扑：唯一名 prod 服务 billing + 双环境同名 web。"""
        home = tmp_path / "vigil_home_envs"
        home.mkdir(parents=True)
        (home / "services").mkdir(parents=True)
        (home / "entities").mkdir(parents=True)
        (home / "runbooks").mkdir(parents=True)
        monkeypatch.setenv("VIGIL_HOME", str(home))
        (home / "topology.yaml").write_text("""
version: 4
environments:
- name: local
- name: prod
clusters:
- name: local
  type: docker
  env: local
  host_groups: []
- name: prod-cluster
  type: k8s
  env: prod
  host_groups: []
hosts:
- name: dev-host-1
  type: host
  env: local
  cluster: local
  endpoint: 172.18.120.67
  os: Ubuntu 24.04
  credentials: []
- name: prod-host-1
  type: host
  env: prod
  cluster: prod-cluster
  endpoint: 10.203.0.9
  os: Ubuntu 24.04
  credentials: []
""", encoding="utf-8")
        (home / "services" / "dev-host-1.yaml").write_text("""
host: dev-host-1
services:
- name: nginx
  type: gateway
  managed_by: docker_compose
  detail: entities/nginx.yaml
- name: web
  type: app
  managed_by: docker
""", encoding="utf-8")
        (home / "services" / "prod-host-1.yaml").write_text("""
host: prod-host-1
services:
- name: billing
  type: db
  managed_by: systemd
- name: web
  type: app
  managed_by: docker
""", encoding="utf-8")
        (home / "entities" / "nginx.yaml").write_text("""
name: nginx
snapshot:
  by_runtime:
    docker_compose:
      project: docker
      services:
      - name: docker-nginx-1
""", encoding="utf-8")
        # template2（local=全 execute 除高危 4；prod 大面积 required）——
        # 正是审查报告里的利用形态模板。
        from tools.matrix_data import template_matrix, write_matrix
        write_matrix(template_matrix("template2"), home)
        yield home

    def _resolved_env(self, home, name):
        """步骤 target 在权限语义下的真实 env（cluster env 优先链）。"""
        from tools.topo_tools import load_topology
        from tools.runbook_exec import resolve_target, _resolved_target_env
        topo = load_topology(home)
        return _resolved_target_env(topo, resolve_target(home, topo, name))

    def test_a_local_runbook_prod_target_rejected(self, ehome):
        """(a) env:local + 唯一名 prod 实体 → 步骤拒绝，命令一条不执行。"""
        calls = []
        data = {
            "name": "env-bypass", "title": "EB", "version": 2,
            "kind": "maintenance", "env": "local",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "billing"}}],
        }
        res = execute_runbook(data, home=ehome, runner=_ok_runner(calls))
        assert res["result"] == "failed"
        step = res["steps"][0]
        assert step["status"] == "failed"
        assert "env" in step["error"] and "prod" in step["error"]
        assert "超出" in step["error"]
        assert calls == [], "范围外实体必须拒绝在任何命令执行之前"
        assert (ehome / "runtime" / "runbook_executions.jsonl").is_file()

    def test_a_legacy_env_alias_still_rejected(self, ehome):
        """声明 env:uat（归一化 prod 档）target local 实体 → 同样拒绝
        （归一化比较，与校验器 clusters/hosts scope 同语义）。"""
        data = {
            "name": "uat-scope", "title": "US", "version": 2,
            "kind": "maintenance", "env": "uat",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "nginx"}}],
        }
        res = execute_runbook(data, home=ehome, runner=_ok_runner())
        assert res["result"] == "failed"
        assert res["steps"][0]["status"] == "failed"
        assert "local" in res["steps"][0]["error"]

    def test_b_same_name_entities_disambiguated_by_scope(self, ehome):
        """(b) 双环境同名 web：env/cluster scope 收敛到对应实体后一致放行。
        （动作用 verify——template2 prod 档仅查询类 execute，restart@prod 会被
        矩阵门拦，见 test_gate_env_follows_entity_env_when_no_declared_env。）"""
        calls = []
        data_local = {
            "name": "web-local", "title": "WL", "version": 2,
            "kind": "maintenance", "env": "local",
            "steps": [{"id": "s", "title": "s", "action": "verify",
                       "params": {"target": "web"}}],
        }
        res = execute_runbook(data_local, home=ehome, runner=_ok_runner(calls))
        assert res["result"] == "ok"
        assert res["steps"][0]["target"]["env"] == "local"
        assert res["steps"][0]["target"]["host"] == "dev-host-1"

        data_prod = {
            "name": "web-prod", "title": "WP", "version": 2,
            "kind": "maintenance", "env": "prod",
            "clusters": ["prod-cluster"],
            "steps": [{"id": "s", "title": "s", "action": "verify",
                       "params": {"target": "web"}}],
        }
        res2 = execute_runbook(data_prod, home=ehome, runner=_ok_runner(calls))
        assert res2["result"] == "ok"
        assert res2["steps"][0]["target"]["env"] == "prod"
        assert res2["steps"][0]["target"]["host"] == "prod-host-1"

    def test_c_matching_env_unchanged(self, ehome):
        """(c) env 匹配实体 env → 放行不变（local runbook + local nginx）。"""
        calls = []
        data = {
            "name": "match", "title": "M", "version": 2,
            "kind": "maintenance", "env": "local",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "nginx"}}],
        }
        res = execute_runbook(data, home=ehome, runner=_ok_runner(calls))
        assert res["result"] == "ok"
        assert res["steps"][0]["status"] == "ok"
        assert len(calls) == 1

    def _allow_runbook_action(self, home):
        """template2 不配 runbook 动作（漏配默认 approve）——嵌套引用测试把它
        设为 execute，避免父引用步骤被审批门拦住（与本测试无关的维度）。"""
        from tools.matrix_data import load_matrix, set_level, write_matrix
        m = load_matrix(home)
        set_level(m, "local", "runbook", "execute")
        write_matrix(m, home)

    def test_d_sub_runbook_env_chain_enforced(self, ehome):
        """(d) 父 env:local → 子 runbook（未声明 env，继承 local）步骤 target
        prod 实体 → 子步骤拒绝、父引用步骤失败。"""
        import yaml
        child = {
            "name": "child-rb", "title": "C", "version": 2,
            "kind": "maintenance",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "billing"}}],
        }
        (ehome / "runbooks" / "child-rb.yaml").write_text(
            yaml.safe_dump(child, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        self._allow_runbook_action(ehome)
        data = {
            "name": "parent-rb", "title": "P", "version": 2,
            "kind": "maintenance", "env": "local",
            "steps": [{"id": "nest", "title": "n", "action": "runbook",
                       "params": {"ref": "child-rb", "type": "runbook"}}],
        }
        calls = []
        res = execute_runbook(data, home=ehome, runner=_ok_runner(calls))
        assert res["result"] == "failed"
        nest = res["steps"][0]
        assert nest["action"] == "runbook" and nest["ok"] is False
        sub_steps = nest.get("sub_steps") or []
        assert sub_steps and sub_steps[0]["status"] == "failed"
        assert "prod" in sub_steps[0]["error"]
        assert calls == [], "子 runbook 的范围外步骤不得产生任何命令"

    def test_d_child_declared_env_conflict_still_rejected(self, ehome):
        """(d) 子 runbook 显式声明 env:prod 超出父 local 范围 → 既有
        _merge_sub_scope 约束仍然生效（范围合并先于步骤执行）。"""
        import yaml
        child = {
            "name": "child-esc", "title": "CE", "version": 2,
            "kind": "maintenance", "env": "prod",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "billing"}}],
        }
        (ehome / "runbooks" / "child-esc.yaml").write_text(
            yaml.safe_dump(child, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        self._allow_runbook_action(ehome)
        data = {
            "name": "parent-esc", "title": "PE", "version": 2,
            "kind": "maintenance", "env": "local",
            "steps": [{"id": "nest", "title": "n", "action": "runbook",
                       "params": {"ref": "child-esc", "type": "runbook"}}],
        }
        res = execute_runbook(data, home=ehome, runner=_ok_runner())
        assert res["result"] == "failed"
        assert "超出父范围" in (res["steps"][0].get("error") or "")

    def test_gate_env_follows_entity_env_when_no_declared_env(self, ehome):
        """未声明 env 的 runbook：scope 检查不触发（"默认不限制"语义），但
        矩阵门 env 跟随实体真实 env——prod 实体按 prod 档裁决（template2 下
        restart=required），无人在场 fail-closed blocked。修复前同一 runbook
        按缺省 local 档（restart=execute）自动放行。"""
        data = {
            "name": "no-env", "title": "NE", "version": 2,
            "kind": "maintenance",
            "steps": [{"id": "s", "title": "s", "action": "restart",
                       "params": {"target": "billing"}}],
        }
        calls = []
        res = execute_runbook(data, home=ehome, runner=_ok_runner(calls))
        step = res["steps"][0]
        # required 档无人在场 → blocked；命令一条未跑（local 档会 execute 放行）
        assert step["status"] == "blocked"
        assert res["result"] == "failed"
        assert calls == []

    def test_rollback_step_env_scope_enforced(self, ehome):
        """回滚场景步骤走同一 choke point：local runbook 失败触发的回滚里，
        target prod 实体的回滚步骤同样拒绝 → 回滚失败强制 stop（人工介入）。
        （范围检查先于回滚步骤的强制人工确认，无需审批桩。）"""
        data = {
            "name": "rb-scope", "title": "RS", "version": 2,
            "kind": "maintenance", "env": "local",
            "steps": [{"id": "q", "title": "q", "action": "query",
                       "params": {"pattern": "x"},
                       "on_failure": {"rollback": "rb-main"}}],
            "rollback": [{"name": "rb-main", "steps": [
                {"id": "r", "title": "r", "action": "restart",
                 "params": {"target": "billing"}}]}],
        }
        res = execute_runbook(data, home=ehome, runner=lambda spec, target: {
            "exit_code": 1, "stdout": "", "stderr": "boom"})
        # 回滚已触发但其中的范围外步骤被拒 → 回滚失败 → 强制 stop（人工介入）。
        assert res["result"] == "failed"
        assert res["rolled_back"] is True  # 回滚已触发（但失败）
        assert "rollback 失败" in (res.get("error") or "")
        rb_block = res["steps"][-1]
        assert rb_block["id"] == "__rollback__"
        assert rb_block["ok"] is False
        rb_step = rb_block["steps"][0]
        assert rb_step["status"] == "failed"
        assert "prod" in rb_step["error"]

    def test_resolved_target_env_chain(self, ehome):
        """_resolved_target_env 优先级链：cluster env > 实体 env > host env。"""
        from tools.topo_tools import load_topology
        from tools.runbook_exec import _resolved_target_env
        topo = load_topology(ehome)
        # billing：实体/host 无独立 env 冲突，cluster prod-cluster env=prod 权威
        assert self._resolved_env(ehome, "billing") == "prod"
        # nginx：cluster local env=local
        assert self._resolved_env(ehome, "nginx") == "local"
        # host 目标：cluster 行 env 权威
        svc = resolve_target(ehome, topo, "prod-host-1")
        assert _resolved_target_env(topo, svc) == "prod"
        # 集群目标自身
        cluster = resolve_target(ehome, topo, "prod-cluster")
        assert _resolved_target_env(topo, cluster) == "prod"


# ---------------------------------------------------------------------------
# task16 M2：transfer_file 远→远直传目标操作数必须是 dst_host
# ---------------------------------------------------------------------------

class TestTransferRemoteToRemote:
    """task16 M2（审查 task12 #M2）：A→B 的 scp 目标操作数此前误用 src_host
    （A→B 实际变成往 A 自身拷贝、源主机名被当作用户名）。修复后目标主机必须是
    dst_host（endpoint 取拓扑 B 行、user 取 B 端凭据）。"""

    def test_dest_operand_uses_dst_host(self, mhome, monkeypatch):
        import subprocess as sp
        import tools.runbook_exec as rex
        import hermes_cli.subcommands.vssh as vssh

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return sp.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        def fake_cred(host, allow_fallback=True):
            assert allow_fallback is False
            return {"type": "ssh_key", "ref": f"~/.ssh/{host}.pem",
                    "user": f"user-{host}", "port": 22}

        def fake_build_ssh_argv(host, *, user="", port=22, key=None, cred=None):
            # vssh 真实形态：argv 尾元素是 user@host 目标（scp 拼装依赖此约定）。
            return (["ssh", "-o", "IdentitiesOnly=yes", "-p", str(port),
                     f"{user}@{host}"], {"VIGIL_TEST": "1"})

        monkeypatch.setattr(vssh, "_resolve_topology_credential", fake_cred)
        monkeypatch.setattr(vssh, "_build_ssh_argv", fake_build_ssh_argv)
        monkeypatch.setattr(
            rex, "_host_endpoint",
            lambda host: "203.0.113.9" if host == "dst-host-b" else "203.0.113.8")
        monkeypatch.setattr(sp, "run", fake_run)

        # _exec_transfer 接收 transfer 规格本体（_run_spec 剥掉 "transfer" 键）
        spec = {
            "source": {"host": "src-host-a", "path": "/data/a.tar.gz"},
            "dest": {"host": "dst-host-b", "path": "/backup/a.tar.gz"},
        }
        res = rex._exec_transfer(mhome, spec)
        assert res["exit_code"] == 0, res.get("stderr")
        argv = captured["argv"]
        assert argv[0] == "scp"
        src_operand, dest_operand = argv[-2], argv[-1]
        # 源操作数 = A 端 user@host:path
        assert src_operand == "user-src-host-a@src-host-a:/data/a.tar.gz"
        # 目标操作数 = B 端 user@B 的 endpoint:path——主机必须是 dst_host
        assert dest_operand == "user-dst-host-b@203.0.113.9:/backup/a.tar.gz"
        assert "src-host-a" not in dest_operand
        assert "203.0.113.8" not in dest_operand

    def test_dest_user_falls_back_root_without_dst_cred(self, mhome, monkeypatch):
        """B 端无拓扑凭据 → user 回退 root（不新增硬前置），主机仍取 dst。"""
        import subprocess as sp
        import tools.runbook_exec as rex
        import hermes_cli.subcommands.vssh as vssh

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return sp.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        def fake_cred(host, allow_fallback=True):
            if host == "dst-host-b":
                return None  # B 端凭据缺失
            return {"type": "ssh_key", "ref": f"~/.ssh/{host}.pem",
                    "user": f"user-{host}", "port": 22}

        monkeypatch.setattr(vssh, "_resolve_topology_credential", fake_cred)
        monkeypatch.setattr(
            vssh, "_build_ssh_argv",
            lambda host, *, user="", port=22, key=None, cred=None:
                (["ssh", f"{user}@{host}"], {}))
        monkeypatch.setattr(rex, "_host_endpoint", lambda host: "203.0.113.9")
        monkeypatch.setattr(sp, "run", fake_run)

        spec = {
            "source": {"host": "src-host-a", "path": "/a"},
            "dest": {"host": "dst-host-b", "path": "/b"},
        }
        res = rex._exec_transfer(mhome, spec)
        assert res["exit_code"] == 0, res.get("stderr")
        assert captured["argv"][-1] == "root@203.0.113.9:/b"


# ---------------------------------------------------------------------------
# task17 — ledger params/trigger_context 落盘前递归 redact（凭据明文永不落盘）
# ---------------------------------------------------------------------------

class TestLedgerRedaction:
    SECRET = "sup3r-secret-token-xyz"

    def test_secret_param_redacted_in_ledger(self, mhome):
        """(a) params 携带 secret 形态值 → ledger JSONL 不含明文。"""
        data = _rb()
        data["steps"].append(
            {"id": "q", "title": "q", "action": "query",
             "params": {"target": "nginx", "pattern": f"password={self.SECRET}"}})
        res = execute_runbook(data, home=mhome, runner=_ok_runner())
        assert res["result"] == "ok"
        raw = ledger_path(mhome).read_text(encoding="utf-8")
        assert self.SECRET not in raw
        rows = recent_executions(home=mhome)
        top = next(r for r in rows if r["runbook"] == "t")
        q_step = next(s for s in top["steps"] if s["id"] == "q")
        assert q_step["params"]["pattern"] != f"password={self.SECRET}"

    def test_trigger_context_secret_redacted_in_ledger(self, mhome):
        """(b) trigger_context 携带敏感告警值 → ledger 不含明文。"""
        data = {
            "name": "tctx", "title": "TCTX", "version": 2, "kind": "incident",
            "env": "local",
            "steps": [{"id": "q", "title": "q", "action": "query",
                       "params": {"target": "nginx",
                                  "pattern": "{{ trigger_context.alertname }}"}}],
        }
        res = execute_runbook(
            data, home=mhome, runner=_ok_runner(),
            trigger_context={"alertname": f"TokenLeak token={self.SECRET}",
                             "severity": "critical"})
        assert res["result"] == "ok"
        raw = ledger_path(mhome).read_text(encoding="utf-8")
        assert self.SECRET not in raw
        rows = recent_executions(home=mhome)
        top = next(r for r in rows if r["runbook"] == "tctx")
        # 键保留、形状完整：非敏感字段原值、敏感字段已打码
        assert top["trigger_context"]["severity"] == "critical"
        assert "alertname" in top["trigger_context"]
        assert self.SECRET not in str(top["trigger_context"])

    def test_benign_params_round_trip_unchanged(self, mhome):
        """(c) 普通 params（路径/端口/标签形态）脱敏后原值落盘——无误伤。"""
        data = _rb()
        data["steps"].append(
            {"id": "q", "title": "q", "action": "query",
             "params": {"target": "nginx",
                        "pattern": "port=9090 host=grafana.local prefix=/metrics"}})
        res = execute_runbook(data, home=mhome, runner=_ok_runner())
        assert res["result"] == "ok"
        rows = recent_executions(home=mhome)
        top = next(r for r in rows if r["runbook"] == "t")
        q_step = next(s for s in top["steps"] if s["id"] == "q")
        assert q_step["params"]["pattern"] == (
            "port=9090 host=grafana.local prefix=/metrics")
        backup = next(s for s in top["steps"] if s["id"] == "backup")
        assert backup["params"]["dest"] == "/backup/nginx/config/latest"

    def test_ledger_line_parseable_keys_intact(self, mhome):
        """(d) 每行 JSON 可解析，顶层/步骤键形状不变（下游读取端兼容）。"""
        execute_runbook(_rb(), home=mhome, runner=_ok_runner())
        lines = ledger_path(mhome).read_text(encoding="utf-8").strip().splitlines()
        assert lines
        for line in lines:
            row = json.loads(line)
            for key in ("ts", "runbook", "env", "trigger_context", "result",
                        "steps", "duration_s", "operator"):
                assert key in row, key
            for step in row["steps"]:
                for key in ("id", "action", "status", "params"):
                    assert key in step, key

    def test_sub_runbook_trigger_context_redacted(self, mhome):
        """子 runbook 账本条目的 trigger_context 同样脱敏（1325 同源）。"""
        from tools.runbook_exec import _run_sub_runbook
        sub_data = {
            "name": "sub", "title": "SUB", "version": 2, "kind": "incident",
            "env": "local",
            "steps": [{"id": "q", "title": "q", "action": "query",
                       "params": {"target": "nginx", "pattern": "x"}}],
        }
        _run_sub_runbook(
            "sub", sub_data, env="local", home=mhome,
            trigger_ctx={"alertname": f"Leak token={self.SECRET}"},
            runner=lambda spec, target: {"exit_code": 0, "stdout": "200", "stderr": ""},
            approve=lambda *a, **k: None, parent_scope=None,
            scheduled=False, exec_id=None, emit=None,
        )
        raw = ledger_path(mhome).read_text(encoding="utf-8")
        assert self.SECRET not in raw
        rows = recent_executions(home=mhome)
        sub_row = next(r for r in rows if r["runbook"] == "sub")
        assert sub_row["trigger_context"]["alertname"] != f"Leak token={self.SECRET}"


# ---------------------------------------------------------------------------
# batch94 PART E — run_script 远端路线（scp 上传 + ssh 执行 + 清理）
# ---------------------------------------------------------------------------

def _write_asset(home, name, content="#!/bin/bash\necho ok\n"):
    """直写资产 + 预审标记（script_asset_create 走 tirith/审批门，测试直写）。"""
    from tools.script_assets import meta_dir, scripts_dir
    sd = scripts_dir(home)
    sd.mkdir(parents=True, exist_ok=True)
    path = sd / f"{name}.sh"
    path.write_text(content, encoding="utf-8")
    md = meta_dir(home)
    md.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    (md / f"{path.name}.json").write_text(json.dumps({
        "approved_at": "2026-09-06T00:00:00+08:00",
        "approved_by": "tester",
        "approved_version": content_hash,
    }), encoding="utf-8")
    return path


class TestScriptAssetRemote:
    def _stub_transport(self, monkeypatch):
        """打桩 ssh/scp：记录 argv，不发真实网络。返回 (calls, scp_calls)。"""
        import subprocess as _sp
        from tools import runbook_exec as rex
        from tools import sudo_tool as st
        calls, scp_calls = [], []

        def _fake_ssh_run(ssh_argv, ssh_env, remote_cmd, timeout=60):
            calls.append({"argv": list(ssh_argv), "cmd": remote_cmd})
            return _sp.CompletedProcess(list(ssh_argv) + [remote_cmd], 0,
                                        stdout="remote-ok", stderr="")

        def _fake_run(argv, **kwargs):
            scp_calls.append([str(a) for a in argv])
            return _sp.CompletedProcess(list(argv), 0, stdout="", stderr="")

        monkeypatch.setattr(rex, "_remote_ssh_argv",
                            lambda target: (["ssh", "-o", "IdentitiesOnly=yes",
                                             "-p", "22", "root@node2"], {}, "node2"))
        monkeypatch.setattr(rex.subprocess, "run", _fake_run)
        monkeypatch.setattr(st, "_ssh_run", _fake_ssh_run)
        return calls, scp_calls

    def test_remote_target_builds_scp_then_ssh_to_target_host(self, mhome, monkeypatch):
        from tools import runbook_exec as rex
        path = _write_asset(mhome, "disk-diag")
        calls, scp_calls = self._stub_transport(monkeypatch)

        res = rex._run_spec(mhome, {"host": "node2", "remote": True},
                            {"script_asset": "disk-diag", "args": ["--fast"]})

        assert res["exit_code"] == 0
        assert res["stdout"] == "remote-ok"
        # scp：目的端 = target 主机解析出的 user@host，源 = 本机资产路径
        assert scp_calls and scp_calls[0][0] == "scp"
        assert scp_calls[0][-2] == str(path)
        assert scp_calls[0][-1].startswith("root@node2:")
        # ssh 执行：chmod 0700 + bash 远端临时唯一路径 + 参数逐词引用
        assert len(calls) == 2
        exec_cmd = calls[0]["cmd"]
        import re as _re
        m = _re.search(r"vigil-script-[0-9a-f]{32}\.sh", exec_cmd)
        assert m, exec_cmd
        tmp = m.group(0)
        assert exec_cmd.startswith(f"chmod 700 /tmp/{tmp} && bash /tmp/{tmp}")
        assert " --fast" in exec_cmd
        # 清理：rm -f 同一远端临时路径
        assert calls[1]["cmd"] == f"rm -f /tmp/{tmp}"

    def test_cleanup_runs_even_when_remote_exec_fails(self, mhome, monkeypatch):
        import subprocess as _sp
        from tools import runbook_exec as rex
        from tools import sudo_tool as st
        _write_asset(mhome, "disk-diag")
        calls = []

        def _fail_then_record(ssh_argv, ssh_env, remote_cmd, timeout=60):
            if remote_cmd.startswith("rm -f"):
                calls.append({"cmd": remote_cmd})
                return _sp.CompletedProcess([], 0, stdout="", stderr="")
            calls.append({"cmd": remote_cmd})
            raise _sp.TimeoutExpired(cmd="ssh", timeout=timeout)

        monkeypatch.setattr(rex, "_remote_ssh_argv",
                            lambda target: (["ssh", "-p", "22", "root@node2"], {}, "node2"))
        monkeypatch.setattr(rex.subprocess, "run",
                            lambda argv, **k: _sp.CompletedProcess(list(argv), 0))
        monkeypatch.setattr(st, "_ssh_run", _fail_then_record)

        res = rex._run_spec(mhome, {"host": "node2", "remote": True},
                            {"script_asset": "disk-diag"})
        assert res["exit_code"] == 1 and res.get("timed_out") is True
        assert [c["cmd"] for c in calls][0].startswith("chmod 700")
        assert [c["cmd"] for c in calls][-1].startswith("rm -f /tmp/vigil-script-")

    def test_scp_failure_reports_asset_name_without_local_path(self, mhome, monkeypatch):
        import subprocess as _sp
        from tools import runbook_exec as rex
        _write_asset(mhome, "disk-diag")

        def _fail_run(argv, **kwargs):
            return _sp.CompletedProcess(list(argv), 1, stdout="", stderr="Permission denied")

        monkeypatch.setattr(rex, "_remote_ssh_argv",
                            lambda target: (["ssh", "-p", "22", "root@node2"], {}, "node2"))
        monkeypatch.setattr(rex.subprocess, "run", _fail_run)

        res = rex._run_spec(mhome, {"host": "node2", "remote": True},
                            {"script_asset": "disk-diag"})
        assert res["exit_code"] == 1
        assert "disk-diag" in res["stderr"]
        assert str(mhome) not in res["stderr"]  # 本地路径不进报错（可能内嵌值）

    def test_local_target_unchanged(self, mhome, monkeypatch):
        """local target：行为逐字节不变——本机 subprocess bash <资产路径>，无 scp/ssh。"""
        import subprocess as _sp
        from tools import runbook_exec as rex
        from tools import sudo_tool as st
        path = _write_asset(mhome, "disk-diag")
        local_calls, ssh_calls = [], []

        def _fake_run(argv, **kwargs):
            local_calls.append([str(a) for a in argv])
            return _sp.CompletedProcess(list(argv), 0, stdout="local-ok", stderr="")

        monkeypatch.setattr(rex.subprocess, "run", _fake_run)
        monkeypatch.setattr(st, "_ssh_run",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("local target must not ssh")))

        res = rex._run_spec(mhome, {"remote": False},
                            {"script_asset": "disk-diag", "args": ["x"]})
        assert res == {"exit_code": 0, "stdout": "local-ok", "stderr": ""}
        assert local_calls == [["bash", str(path), "x"]]

    def test_matrix_gate_applies_to_remote_script_steps(self, mhome, monkeypatch):
        """(d) 审批/矩阵路径对远端步骤不变：run_script=required 时远端目标同样
        被门拦截，runner（→ scp/ssh）根本不被触达。"""
        from tools import runbook_exec as rex
        from tools import sudo_tool as st
        from tools.matrix_data import write_matrix
        _write_asset(mhome, "disk-diag")
        # node2：公网占位 endpoint → resolve 为 remote 目标
        (mhome / "topology.yaml").write_text("""
version: 4
environments:
- name: local
clusters:
- name: local
  type: docker
  env: local
  host_groups: []
hosts:
- name: node2
  type: host
  env: local
  cluster: local
  endpoint: 203.0.113.44
  os: Ubuntu 24.04
  credentials: []
""", encoding="utf-8")
        write_matrix({
            "schema_version": 1,
            "matrix": {"local": {"run_script": "required"}},
        }, mhome)

        def _no_transport(*a, **k):
            raise AssertionError("blocked step must not reach scp/ssh")

        monkeypatch.setattr(st, "_ssh_run", _no_transport)
        monkeypatch.setattr(rex, "_remote_ssh_argv", _no_transport)

        res = rex.execute_runbook({
            "name": "t-rs", "title": "T", "version": 2, "kind": "maintenance",
            "env": "local", "on_failure": "stop",
            "steps": [{"id": "s1", "title": "远端脚本", "action": "run_script",
                       "params": {"script": "disk-diag", "target": "node2"}}],
        }, home=mhome, runner=lambda spec, target: {
            "exit_code": 0, "stdout": "ok", "stderr": ""})
        # 步骤级 blocked → runbook 级 failed 收场，错误指向强制人工门；
        # 关键不变量：门在 runner 之前，scp/ssh 传输层根本未被触达。
        assert res["result"] in ("blocked", "failed")
        assert "需要人工审批" in res["error"]
        assert "run_script@local" in res["error"]
