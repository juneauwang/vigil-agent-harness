"""batch87 任务 3c/4 —— `vigil alerts` CLI（OPS-DELTA #103）。

覆盖验收：k CLI 输出告警 + 建议（文本与 --json，含无匹配告警）；未配置
Alertmanager 明确报错非零退出；history 回看 triage 审计（文本/--json，n 可查）。
handler 通过 argparse Namespace 直调（hermes_cli.alerts_cli.run_alerts_command）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.config as hc
from hermes_cli.alerts_cli import run_alerts_command
from tools import alert_runbook as ar

HARBOR = """\
name: harbor-restart
title: Harbor 服务异常恢复
kind: incident
env: prod
triggers:
  - harbor healthcheck failed
steps: [{id: s1, title: x, commands: [a]}]
"""


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "runbooks" / "harbor-restart.yaml").write_text(HARBOR, encoding="utf-8")
    (home / "config.yaml").write_text(
        "ops:\n"
        "  prometheus:\n"
        "    endpoint: http://127.0.0.1:9090\n"
        "    alertmanager: http://127.0.0.1:9093\n"
        "    vault_path: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _am_alerts(monkeypatch):
    from tools import prom_tools as pt

    payload = [
        {"status": {"state": "active"},
         "labels": {"alertname": "Harbor healthcheck failed", "severity": "critical"},
         "annotations": {}, "startsAt": "2026-08-23T10:00:00Z"},
        {"status": {"state": "active"},
         "labels": {"alertname": "DiskFull", "severity": "warning"},
         "annotations": {}, "startsAt": "2026-08-23T09:00:00Z"},
    ]
    monkeypatch.setattr(pt, "_http_get", lambda url, params, auth: _FakeResp(payload))


# --- k：文本输出（告警 + 建议，含无匹配） ---

def test_list_text_alerts_and_suggestions(cli_home, capsys, monkeypatch):
    _am_alerts(monkeypatch)
    rc = run_alerts_command(SimpleNamespace(alerts_command=None, json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "活跃告警: 2" in out
    assert "1 条有 runbook 建议" in out
    assert "[critical] Harbor healthcheck failed" in out
    assert "harbor-restart" in out
    assert "高置信 · 触发词命中" in out
    assert "DiskFull" in out
    assert "无匹配 runbook" in out


# --- k：--json 机器可读 ---

def test_list_json_machine_readable(cli_home, capsys, monkeypatch):
    _am_alerts(monkeypatch)
    rc = run_alerts_command(SimpleNamespace(alerts_command="list", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["count"] == 2
    assert payload["matched_count"] == 1
    by_name = {a["alertname"]: a for a in payload["alerts"]}
    assert by_name["Harbor healthcheck failed"]["disposition"]["runbook"] == "harbor-restart"
    assert by_name["DiskFull"]["disposition"]["matched"] is False


# --- 未配置 Alertmanager → 明确报错非零退出 ---

def test_unconfigured_alertmanager_error(cli_home, capsys, monkeypatch):
    from tools import prom_tools as pt

    monkeypatch.setattr(
        pt, "_prom_config",
        lambda: {"endpoint": "http://127.0.0.1:9090", "alertmanager": "", "vault_path": ""},
    )
    rc = run_alerts_command(SimpleNamespace(alerts_command=None, json=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "未配置 Alertmanager" in out


# --- n：history 回看审计（文本 + JSON） ---

def test_history_text_and_json(cli_home, capsys, monkeypatch):
    ar.triage_alerts(
        [{"alertname": "Harbor healthcheck failed", "severity": "critical",
          "labels": {}, "summary": "", "instance": ""},
         {"alertname": "DiskFull", "severity": "warning",
          "labels": {}, "summary": "", "instance": ""}],
        home=cli_home,
    )
    rc = run_alerts_command(SimpleNamespace(alerts_command="history", json=False, limit=10))
    out = capsys.readouterr().out
    assert rc == 0
    assert "最近 triage 审计" in out
    assert "告警 2 条（匹配 1）" in out
    assert "Harbor healthcheck failed" in out
    assert "harbor-restart" in out
    assert "DiskFull" in out and "无匹配" in out

    rc = run_alerts_command(SimpleNamespace(alerts_command="history", json=True, limit=10))
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(rows) == 1
    assert rows[0]["entries"][0]["runbook"] == "harbor-restart"
    # n：文件路径稳定可读
    assert ar.alert_triage_audit_path(cli_home).is_file()
    assert ar.alert_triage_audit_path(cli_home).name == "alert_triage.jsonl"
