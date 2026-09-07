"""任务25 —— 告警驱动的 runbook 自动派发（autonomous duty loop）验收测试。

覆盖：验证器（alert_auto_run 必须 bool / 无 triggers 拒绝 / 与 schedule 互斥 /
severity 白名单形态）；派发 tick（默认门关不动作；授权命中经 scheduled 豁免
通道执行 + 审计；未授权命中维持建议闭环；幂等 occurrence 键；severity 白名单
跳过；单 tick 上限；预审缺失 → 引擎拒绝 → needs-human 降级；失败升级
needs-human 且建议仍在）；cron 注册幂等（task18 topo_sync 同款）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import hermes_cli.config as hc
import hermes_cli.alert_autodispatch as ad


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_AUTHORIZED_RB = """\
name: harbor-restart
title: Harbor 自动恢复
version: 2
kind: incident
env: local
alert_auto_run: true
triggers:
  - {alertname: HarborHealthcheckDown, severity: critical}
steps:
- id: s1
  title: 诊断
  action: query
  params: {target: harbor}
"""

_PLAIN_RB = """\
name: manual-only
title: 仅建议 SOP
version: 2
kind: incident
env: local
triggers:
  - {alertname: DiskFull, severity: warning}
steps:
- id: s1
  title: 探查
  action: query
  params: {target: harbor}
"""


@pytest.fixture
def ahome(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 最小拓扑（harbor 服务）+ 清配置缓存。"""
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "runbooks").mkdir()
    (home / "services").mkdir()
    (home / "entities").mkdir()
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
- name: node1
  type: host
  env: local
  cluster: local
  endpoint: 127.0.0.1
  os: Ubuntu 24.04
  credentials: []
""", encoding="utf-8")
    (home / "services" / "node1.yaml").write_text("""
host: node1
services:
- name: harbor
  type: registry
  managed_by: docker_compose
  source: manual
""", encoding="utf-8")
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _enable(monkeypatch, **over):
    cfg = {"enabled": True, "interval": "5m", "max_per_tick": 3}
    cfg.update(over)
    monkeypatch.setattr(hc, "load_config_readonly",
                        lambda: {"ops": {"alerts": {"auto_dispatch": cfg}}})


def _write(home: Path, name: str, body: str) -> None:
    (home / "runbooks" / f"{name}.yaml").write_text(body, encoding="utf-8")


def _preapprove(home: Path, name: str, body: str) -> None:
    """给 runbook 文件落预审标记（内容哈希匹配）——authoring-time 授权语义。"""
    from tools.runbook_tools import _load_runbook
    data = _load_runbook(home, name)
    payload = {k: v for k, v in data.items()
               if k not in ("approved_at", "approved_by", "approved_version")}
    version = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   default=str).encode()).hexdigest()[:16]
    marked = body.rstrip("\n") + (
        f"\napproved_at: 2026-09-06T00:00:00+08:00\n"
        f"approved_by: author\napproved_version: {version}\n")
    _write(home, name, marked)


def _alert(**kw):
    base = {"alertname": "HarborHealthcheckDown", "severity": "critical",
            "instance": "harbor:443", "labels": {}, "annotations": {},
            "summary": "", "startsAt": "2026-09-06T03:00:00Z", "state": "active"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 验证器（T2/f：alert_auto_run 校验）
# ---------------------------------------------------------------------------

class TestValidator:
    def _validate(self, data):
        from tools.runbook_tools import _validate_alert_auto_run
        _validate_alert_auto_run(data, "x")

    def test_requires_bool(self):
        with pytest.raises(ValueError, match="布尔值"):
            self._validate({"alert_auto_run": "yes", "triggers": ["down"]})

    def test_false_is_noop_without_triggers(self):
        self._validate({"alert_auto_run": False})

    def test_requires_triggers(self):
        with pytest.raises(ValueError, match="没有 triggers"):
            self._validate({"alert_auto_run": True, "steps": []})

    def test_mutex_with_schedule(self):
        with pytest.raises(ValueError, match="互斥"):
            self._validate({"alert_auto_run": True, "triggers": ["down"],
                            "schedule": {"cron": "* * * * *", "timezone": "UTC"}})

    def test_severity_whitelist_shape(self):
        self._validate({"alert_auto_run": True, "triggers": ["down"],
                        "alert_auto_severity": ["critical"]})
        with pytest.raises(ValueError, match="alert_auto_severity"):
            self._validate({"alert_auto_run": True, "triggers": ["down"],
                            "alert_auto_severity": "critical"})
        with pytest.raises(ValueError, match="alert_auto_severity"):
            self._validate({"alert_auto_run": True, "triggers": ["down"],
                            "alert_auto_severity": []})

    def test_engine_rejects_unauthorized_declaration(self, ahome):
        """引擎校验：alert_auto_run 无 triggers 的 runbook 执行前被拒
        （校验在 runbook 执行/创建 choke point，加载只归一化不校验）。"""
        _write(ahome, "bad", """\
name: bad
title: Bad
version: 2
kind: incident
alert_auto_run: true
steps:
- id: s1
  title: x
  action: query
  params: {target: harbor}
""")
        from tools.runbook_exec import execute_runbook
        from tools.runbook_tools import _load_runbook
        data = _load_runbook(ahome, "bad")
        # 引擎把校验错误转为 blocked 结果（fail-closed，不执行任何步骤）
        res = execute_runbook(data, home=ahome)
        assert res["result"] == "blocked"
        assert "没有 triggers" in res["error"]


# ---------------------------------------------------------------------------
# 派发 tick（T3-T5）
# ---------------------------------------------------------------------------

class TestRunOnce:
    def test_gate_default_off_never_fetches(self, ahome, monkeypatch):
        """(g) 默认关：门关着连告警拉取都不发生。"""
        def _boom():
            raise AssertionError("gate is off; must not fetch")
        out = ad.run_once(ahome, fetch_alerts=_boom)
        assert "未启用" in out and "跳过" in out

    def test_authorized_dispatch_via_scheduled_exemption(self, ahome, monkeypatch):
        """(b) 授权 runbook 命中 → scheduled 豁免通道执行（逐步骤审批被豁免，
        引擎只认预审标记 + 内容哈希）；trigger_context 带告警信息落 ledger。"""
        _enable(monkeypatch)
        _write(ahome, "harbor-restart", _AUTHORIZED_RB)
        _preapprove(ahome, "harbor-restart", _AUTHORIZED_RB)

        ran = {}
        def _fake_run_spec(home, target, spec):
            ran["spec"] = spec
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}

        monkeypatch.setattr("tools.runbook_exec._run_spec", _fake_run_spec)
        out = ad.run_once(ahome, fetch_alerts=lambda: [_alert()])
        assert "已自动执行" in out
        assert ran.get("spec"), "engine must have executed the runbook step"

        from tools.runbook_exec import recent_executions
        rows = recent_executions(ahome)
        assert rows and rows[0]["source"] == "schedule"
        assert rows[0]["trigger_context"].get("trigger") == "alert"
        assert rows[0]["trigger_context"]["alert"]["alertname"] == "HarborHealthcheckDown"

        audit = ad.recent_dispatch_audit(ahome)
        assert audit and audit[0]["needs_human"] is False
        assert audit[0]["runbook"] == "harbor-restart"
        assert audit[0]["matched_keyword"]

    def test_unauthorized_never_runs(self, ahome, monkeypatch):
        """(a) 未授权 runbook 命中 → 绝不执行，维持建议闭环。"""
        _enable(monkeypatch)
        _write(ahome, "manual-only", _PLAIN_RB)
        _preapprove(ahome, "manual-only", _PLAIN_RB)  # 即便预审过：没授权也不行

        def _boom(*a, **k):
            raise AssertionError("unauthorized runbook must not execute")
        monkeypatch.setattr("tools.runbook_exec._run_spec", _boom)

        out = ad.run_once(ahome, fetch_alerts=lambda: [_alert(alertname="DiskFull",
                                                              severity="warning")])
        assert "派发 0 次" in out
        assert ad.recent_dispatch_audit(ahome) == []

    def test_authorized_and_unauthorized_mixed(self, ahome, monkeypatch):
        """混合命中：授权的执行，未授权的维持建议（计数可见）。"""
        _enable(monkeypatch)
        _write(ahome, "harbor-restart", _AUTHORIZED_RB)
        _preapprove(ahome, "harbor-restart", _AUTHORIZED_RB)
        _write(ahome, "manual-only", _PLAIN_RB)
        _preapprove(ahome, "manual-only", _PLAIN_RB)
        monkeypatch.setattr("tools.runbook_exec._run_spec",
                            lambda home, target, spec: {"exit_code": 0, "stdout": "ok", "stderr": ""})
        out = ad.run_once(ahome, fetch_alerts=lambda: [
            _alert(),
            _alert(alertname="DiskFull", severity="warning",
                   startsAt="2026-09-06T04:00:00Z"),
        ])
        assert "已自动执行" in out
        assert "维持建议" in out
        rows = ad.recent_dispatch_audit(ahome)
        assert len(rows) == 1 and rows[0]["runbook"] == "harbor-restart"

    def test_idempotent_per_occurrence(self, ahome, monkeypatch):
        """(c) 同 occurrence（alertname|instance|startsAt）只派发一次；
        startsAt 变化（恢复后重燃）→ 可再次派发。"""
        _enable(monkeypatch)
        _write(ahome, "harbor-restart", _AUTHORIZED_RB)
        _preapprove(ahome, "harbor-restart", _AUTHORIZED_RB)
        monkeypatch.setattr("tools.runbook_exec._run_spec",
                            lambda home, target, spec: {"exit_code": 0, "stdout": "ok", "stderr": ""})

        first = ad.run_once(ahome, fetch_alerts=lambda: [_alert()])
        second = ad.run_once(ahome, fetch_alerts=lambda: [_alert()])
        assert "已自动执行" in first
        assert "不重复派发" in second
        refire = ad.run_once(ahome, fetch_alerts=lambda: [_alert(startsAt="2026-09-06T09:00:00Z")])
        assert "已自动执行" in refire

    def test_severity_whitelist_skips_out_of_scope(self, ahome, monkeypatch):
        body = _AUTHORIZED_RB.replace(
            "triggers:", "alert_auto_severity: [critical]\ntriggers:")
        _write(ahome, "harbor-restart", body)
        _preapprove(ahome, "harbor-restart", body)
        _enable(monkeypatch)
        monkeypatch.setattr("tools.runbook_exec._run_spec",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("out-of-scope severity must not run")))
        out = ad.run_once(ahome, fetch_alerts=lambda: [_alert(severity="warning")])
        assert "白名单" in out
        assert ad.recent_dispatch_audit(ahome) == []

    def test_rate_cap_per_tick(self, ahome, monkeypatch):
        _enable(monkeypatch, max_per_tick=1)
        body = _AUTHORIZED_RB.replace(
            "triggers:", "alert_auto_severity: [critical]\ntriggers:")
        body = body.replace("{alertname: HarborHealthcheckDown, severity: critical}",
                            "{playbook: auto}")
        _write(ahome, "harbor-restart", body)
        _preapprove(ahome, "harbor-restart", body)
        monkeypatch.setattr("tools.runbook_exec._run_spec",
                            lambda home, target, spec: {"exit_code": 0, "stdout": "ok", "stderr": ""})
        alerts = [_alert(instance=f"node{i}:9100",
                         annotations={"playbook": "auto"},
                         startsAt=f"2026-09-06T0{i}:00:00Z") for i in range(1, 4)]
        out = ad.run_once(ahome, fetch_alerts=lambda: alerts)
        assert "派发上限" in out
        rows = ad.recent_dispatch_audit(ahome)
        assert len(rows) == 1

    def test_missing_asset_preapproval_blocked_needs_human(self, ahome, monkeypatch):
        """(d) 授权但未过资产预审 → 引擎 fail-closed 拒绝（不半跑），
        审计标 needs-human；传输层/引擎 runner 不被触达。"""
        _enable(monkeypatch)
        _write(ahome, "harbor-restart", _AUTHORIZED_RB)  # 无预审标记

        def _boom(home, target, spec):
            raise AssertionError("blocked run must not execute steps")
        monkeypatch.setattr("tools.runbook_exec._run_spec", _boom)

        out = ad.run_once(ahome, fetch_alerts=lambda: [_alert()])
        assert "needs-human" in out
        audit = ad.recent_dispatch_audit(ahome)
        assert audit[0]["needs_human"] is True
        assert "资产审批" in audit[0]["error"]
        state = ad._load_state(ahome)
        assert any(v["needs_human"] for v in state.values())

    def test_failed_auto_run_escalates_and_suggestion_remains(self, ahome, monkeypatch):
        """(e) 自动执行失败 → 审计标 needs-human；同键不重试风暴；告警仍在
        活跃集 → 建议/triage 照常给出（人兜底，绝不静默吞掉）。"""
        _enable(monkeypatch)
        _write(ahome, "harbor-restart", _AUTHORIZED_RB)
        _preapprove(ahome, "harbor-restart", _AUTHORIZED_RB)
        monkeypatch.setattr("tools.runbook_exec._run_spec",
                            lambda home, target, spec: {"exit_code": 1, "stdout": "",
                                                        "stderr": "boom"})

        out = ad.run_once(ahome, fetch_alerts=lambda: [_alert()])
        assert "needs-human" in out
        audit = ad.recent_dispatch_audit(ahome)
        assert audit[0]["needs_human"] is True

        # 同 occurrence 不重试（failed 也算已消耗的 occurrence）
        again = ad.run_once(ahome, fetch_alerts=lambda: [_alert()])
        assert "不重复派发" in again

        # 建议闭环仍然给出该告警的 SOP（未被自动派发"吞掉"）
        from tools.alert_runbook import match_alert_to_runbook
        disp = match_alert_to_runbook(_alert(), home=ahome)
        assert disp["matched"] is True and disp["runbook"] == "harbor-restart"

    def test_no_active_alerts_is_quiet_ok(self, ahome, monkeypatch):
        _enable(monkeypatch)
        out = ad.run_once(ahome, fetch_alerts=lambda: [])
        assert "派发 0 次" in out


# ---------------------------------------------------------------------------
# cron 注册（task18 topo_sync 同款）
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_and_unregister(self, ahome):
        out = ad.register_alert_autodispatch_schedule("5m", ahome)
        assert out == "registered"
        script = ahome / "runtime" / "alert_autodispatch.sh"
        assert script.is_file()
        assert (script.stat().st_mode & 0o777) == 0o700
        body = script.read_text(encoding="utf-8")
        assert f'VIGIL_HOME="{ahome}"' in body
        assert "-m hermes_cli.alert_autodispatch" in body

        from cron.jobs import list_jobs, use_cron_store
        with use_cron_store(ahome):
            jobs = [j for j in list_jobs(include_disabled=True)
                    if str(j.get("name")) == "alert_autodispatch"]
        assert len(jobs) == 1 and jobs[0]["no_agent"] is True

        # 幂等更新
        assert ad.register_alert_autodispatch_schedule("10m", ahome) == "updated"
        # 注销
        assert ad.register_alert_autodispatch_schedule("off", ahome) == "unregistered"
        assert ad.register_alert_autodispatch_schedule("off", ahome) == "noop"

    def test_config_gate_read(self, monkeypatch):
        monkeypatch.setattr(hc, "load_config_readonly",
                            lambda: {"ops": {"alerts": {"auto_dispatch": {
                                "enabled": True, "interval": "5m",
                                "max_per_tick": "7"}}}})
        cfg = ad.auto_dispatch_config()
        assert cfg["enabled"] is True and cfg["max_per_tick"] == 7
