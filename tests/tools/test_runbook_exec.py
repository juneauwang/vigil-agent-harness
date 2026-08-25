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
        endpoint==host_name 误判本地（2026-08-25 实测：阿里云 39.106.217.32
        endpoint==host_name，旧代码 `e == host_name → local` 导致 kubectl 命令
        在本机执行，runbook 执行器全部走错机器 "timed out waiting for the
        condition"）。本机身份只认 hostname/网卡 IP，不认拓扑 host_name。"""
        from tools.runbook_exec import _is_local_endpoint
        # 远端公网 IP 作 endpoint 且 host_name 同名 → 不是本机身份 → remote
        assert _is_local_endpoint("39.106.217.32", "39.106.217.32") is False
        assert _is_local_endpoint("8.140.60.44", "8.140.60.44") is False
        # 带端口形态同样判 remote
        assert _is_local_endpoint("39.106.217.32:22", "39.106.217.32") is False
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
