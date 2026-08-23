"""YAPL P4（批次五十八）：调度器接线验收测试。

覆盖（任务书 §P4 阶段 4）：
  - cron_gen 工具：自然语言 → cron 确定性转换正确性（每分钟/小时/天、每天
    时段点、每周/每月、中文数字、已给 cron 校验、非法引导）；
  - schedule 注册：runbook_create 带 schedule → cron job 注册（runbook 标记）、
    更新同步、schedule 移除注销；
  - 定时触发（cron.scheduler.run_job runbook 分支）：预审 runbook 豁免执行 +
    触发上下文注入 + 执行记录（事后审计）；未预审 → 拒绝；runbook 缺失 → 报错。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cron import jobs as cron_jobs
from tools import terminal_tool
from tools.cron_gen import cron_gen
from tools.matrix_data import template_matrix, write_matrix
from tools.runbook_exec import recent_executions
from tools.runbook_schedule import register_runbook_schedule, runbook_schedule_status
from tools.runbook_tools import runbook_create


@pytest.fixture
def shome(tmp_path, monkeypatch):
    home = tmp_path / "vigil_home"
    home.mkdir(parents=True)
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    write_matrix(template_matrix("template1"), home)
    yield home


def _preapprove(data: dict) -> dict:
    data = json.loads(json.dumps(data))
    data["approved_at"] = "2026-08-23T00:00:00+08:00"
    data["approved_by"] = "wpwang"
    payload = {k: v for k, v in data.items()
               if k not in ("approved_at", "approved_by", "approved_version")}
    data["approved_version"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   default=str).encode()).hexdigest()[:16]
    return data


def _simple_rb():
    return {
        "name": "sched-t", "title": "T", "version": 2, "kind": "checklist",
        "env": "local",
        "steps": [{"id": "q", "title": "q", "action": "query",
                   "params": {"pattern": "x"}}],
    }


class TestCronGen:
    @pytest.mark.parametrize("text,expected", [
        ("每5分钟", "*/5 * * * *"),
        ("每2小时", "0 */2 * * *"),
        ("每小时", "0 * * * *"),
        ("每天9点", "0 9 * * *"),
        ("每天9:30", "30 9 * * *"),
        ("每天下午3点", "0 15 * * *"),
        ("每天中午12点", "0 12 * * *"),
        ("每周三凌晨2点", "0 2 * * 3"),
        ("每周一 3点15分", "15 3 * * 1"),
        ("每周六晚上8点30分", "30 20 * * 6"),
        ("每月1日3点", "0 3 1 * *"),
        ("每季度", "0 0 1 */3 *"),
        ("0 2 * * 3", "0 2 * * 3"),
    ])
    def test_phrase_to_cron(self, text, expected):
        out = json.loads(cron_gen(text))
        assert out["cron"] == expected

    def test_invalid_guides(self):
        out = cron_gen("随便说说")
        assert "禁止手算" in out
        out = cron_gen("每天二十五点")
        assert "无法解析" in out

    def test_timezone_helper(self):
        out = json.loads(cron_gen("每天9点", tz="Asia/Shanghai"))
        assert out["timezone"] == "Asia/Shanghai"
        assert "now_in_tz" in out
        out = cron_gen("每天9点", tz="Not/AZone")
        assert "IANA" in out


class TestScheduleRegistration:
    def _approve_on(self, monkeypatch):
        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        terminal_tool.set_approval_callback(lambda *a, **k: "once")

    def test_create_with_schedule_registers(self, shome, monkeypatch):
        self._approve_on(monkeypatch)
        try:
            rb = _simple_rb()
            rb["schedule"] = {"cron": "0 2 * * 3", "timezone": "Asia/Shanghai"}
            runbook_create(**{k: v for k, v in rb.items()
                              if k not in ("name", "version")},
                           runbook="sched-t", home=shome)
        finally:
            terminal_tool.set_approval_callback(None)
        status = runbook_schedule_status("sched-t", home=shome)
        assert status is not None and status["job_id"]
        assert status["timezone"] == "Asia/Shanghai"
        jobs = cron_jobs.list_jobs()
        assert any(j.get("runbook") == "sched-t" for j in jobs)

    def test_update_schedule_syncs(self, shome, monkeypatch):
        self._approve_on(monkeypatch)
        try:
            rb = _simple_rb()
            rb["schedule"] = {"cron": "0 2 * * 3", "timezone": "Asia/Shanghai"}
            runbook_create(**{k: v for k, v in rb.items()
                              if k not in ("name", "version")},
                           runbook="sched-t", home=shome)
            rb2 = dict(rb)
            rb2["schedule"] = {"cron": "30 4 * * 1", "timezone": "UTC"}
            out = runbook_create(**{k: v for k, v in rb2.items()
                                    if k not in ("name", "version")},
                                 runbook="sched-t", overwrite=True, home=shome)
            assert "updated" in out
        finally:
            terminal_tool.set_approval_callback(None)
        jobs = [j for j in cron_jobs.list_jobs() if j.get("runbook") == "sched-t"]
        assert len(jobs) == 1  # 更新不产生重复 job
        assert jobs[0]["schedule_display"] == "30 4 * * 1"
        assert jobs[0]["runbook_timezone"] == "UTC"

    def test_schedule_removed_unregisters(self, shome, monkeypatch):
        self._approve_on(monkeypatch)
        try:
            rb = _simple_rb()
            rb["schedule"] = {"cron": "0 2 * * 3", "timezone": "Asia/Shanghai"}
            runbook_create(**{k: v for k, v in rb.items()
                              if k not in ("name", "version")},
                           runbook="sched-t", home=shome)
            rb2 = dict(rb)
            rb2.pop("schedule")
            runbook_create(**{k: v for k, v in rb2.items()
                              if k not in ("name", "version")},
                           runbook="sched-t", overwrite=True, home=shome)
        finally:
            terminal_tool.set_approval_callback(None)
        assert runbook_schedule_status("sched-t", home=shome) is None

    def test_no_schedule_noop(self, shome):
        assert register_runbook_schedule("sched-t", None, shome) == "noop"


class TestScheduledTrigger:
    def _job(self, runbook="sched-t"):
        return {
            "id": "testjob1",
            "name": f"runbook:{runbook}",
            "runbook": runbook,
            "deliver": "local",
            "schedule": {"kind": "cron", "expr": "0 2 * * 3",
                         "display": "0 2 * * 3"},
        }

    def _write_preapproved(self, shome):
        from tools.runbook_tools import _runbook_path
        data = _preapprove(_simple_rb())
        _runbook_path(shome, "sched-t").parent.mkdir(parents=True, exist_ok=True)
        import yaml
        _runbook_path(shome, "sched-t").write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

    def test_preapproved_run_exempt_and_recorded(self, shome, monkeypatch):
        from cron.scheduler import run_job
        self._write_preapproved(shome)
        ok, doc, final, err = run_job(self._job())
        assert ok is True
        assert "sched-t" in final and "Result:" in doc and "ok" in doc
        rows = recent_executions(shome)
        assert rows and rows[0]["result"] == "ok"
        assert rows[0]["source"] == "schedule"
        assert rows[0]["trigger_context"]["schedule"]["cron"] == "0 2 * * 3"

    def test_unapproved_blocked(self, shome, monkeypatch):
        from cron.scheduler import run_job
        from tools.runbook_tools import _runbook_path
        import yaml
        _runbook_path(shome, "sched-t").parent.mkdir(parents=True, exist_ok=True)
        _runbook_path(shome, "sched-t").write_text(
            yaml.safe_dump(_simple_rb(), allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        ok, doc, final, err = run_job(self._job())
        assert ok is False
        assert "未过资产审批" in (final or "") or "未过资产审批" in (err or "")

    def test_runbook_missing(self, shome):
        from cron.scheduler import run_job
        ok, doc, final, err = run_job(self._job("ghost"))
        assert ok is False
        assert "不存在" in (err or "")

    def test_scheduled_exempt_skips_approval(self, shome, monkeypatch):
        """定时执行即使矩阵 required 也不弹审批（豁免路径）。"""
        from cron.scheduler import run_job
        from tools.matrix_data import load_matrix, set_level, write_matrix
        m = load_matrix(shome)
        set_level(m, "local", "query", "required")
        write_matrix(m, shome)
        self._write_preapproved(shome)
        called = []
        terminal_tool.set_approval_callback(
            lambda *a, **k: called.append(1) or "deny")
        try:
            ok, doc, final, err = run_job(self._job())
        finally:
            terminal_tool.set_approval_callback(None)
        assert ok is True
        assert called == []  # 豁免：无审批回调
