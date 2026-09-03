"""batch87 任务 2/3a —— alert_triage agent 工具（OPS-DELTA #103）。

覆盖验收：f 工具描述含"仅供建议、执行需用户确认后调 runbook_execute"约束
（grep 可验证）；g 匹配结果无执行直通逻辑；h 一次调用返回告警全景 + 建议（含无
匹配告警的处理）；l 全程只读——handler 内零 runbook 执行（执行账本文件不产生）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import hermes_cli.config as hc
import tools.prom_tools as pt
from tools.registry import registry

PROM_ENDPOINT = "http://127.0.0.1:9090"
ALERTMANAGER = "http://127.0.0.1:9093"


class _FakeResp:
    def __init__(self, json_data):
        self.status_code = 200
        self._json_data = json_data

    @property
    def text(self):
        return ""

    def json(self):
        return self._json_data


def _am_payload(alerts):
    return [
        {
            "status": {"state": a.get("state", "active")},
            "labels": {"alertname": a["alertname"], "severity": a.get("severity", "warning"),
                       **{k: v for k, v in a.get("labels", {}).items()}},
            "annotations": {"summary": a.get("summary", "")},
            "startsAt": "2026-09-01T10:00:00Z",
        }
        for a in alerts
    ]


HARBOR = """\
name: harbor-restart
title: Harbor 服务异常恢复
kind: incident
env: prod
summary: harbor 健康检查失败时的标准恢复流程。
triggers:
  - harbor healthcheck failed
steps:
  - id: diagnose
    title: 诊断
    commands: ["docker ps --filter name=harbor"]
"""


@pytest.fixture
def triage_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "runbooks" / "harbor-restart.yaml").write_text(HARBOR, encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "ops": {"prometheus": {"endpoint": PROM_ENDPOINT,
                                   "alertmanager": ALERTMANAGER,
                                   "vault_path": ""}},
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def am_up(triage_home, monkeypatch):
    def _install(alerts):
        monkeypatch.setattr(
            pt, "_http_get",
            lambda url, params, auth: _FakeResp(_am_payload(alerts)),
        )
    return _install


# ---------------------------------------------------------------------------
# f — 工具描述含"仅供建议、执行需确认"约束（描述文本 grep 可验证）
# ---------------------------------------------------------------------------

def test_f_tool_description_has_suggestion_constraint():
    schema = registry._tools["alert_triage"].schema
    desc = schema["description"]
    assert "仅供建议" in desc
    assert "绝不自动执行" in desc or "不执行任何 runbook" in desc
    assert "runbook_execute" in desc
    assert "确认" in desc


def test_f_tool_readonly_schema_and_empty_params():
    tool = registry._tools["alert_triage"]
    assert tool.schema["name"] == "alert_triage"
    assert tool.schema["parameters"]["properties"] == {}
    assert tool.emoji  # 有展示元数据


# ---------------------------------------------------------------------------
# g — 匹配结果无执行直通（没有 execute 类字段/入口）
# ---------------------------------------------------------------------------

def test_g_suggestion_rows_have_no_execute_entry(triage_home, am_up):
    am_up([
        {"alertname": "Harbor healthcheck failed", "severity": "critical"},
        {"alertname": "disk full on worker", "severity": "warning"},
    ])
    result = json.loads(pt.alert_triage())
    for row in result["alerts"]:
        keys = set(row)
        assert not any(k.startswith("exec") or k in ("action", "command") for k in keys)
    matched = next(r for r in result["alerts"] if r["matched"])
    assert set(matched).issubset({
        "alertname", "severity", "instance", "matched", "runbook",
        "confidence", "matched_by", "matched_keyword", "reason", "alternatives",
    })


# ---------------------------------------------------------------------------
# h — 一次调用返回全景 + 建议（含无匹配告警处理）
# ---------------------------------------------------------------------------

def test_h_one_call_panorama_with_unmatched(triage_home, am_up):
    am_up([
        {"alertname": "Harbor healthcheck failed", "severity": "critical"},
        {"alertname": "disk full on worker", "severity": "warning",
         "labels": {"instance": "worker-1"}},
    ])
    result = json.loads(pt.alert_triage())
    assert result["count"] == 2
    assert result["matched_count"] == 1
    by_name = {r["alertname"]: r for r in result["alerts"]}
    hit = by_name["Harbor healthcheck failed"]
    assert hit["matched"] is True
    assert hit["runbook"] == "harbor-restart"
    assert hit["confidence"] == "high"
    assert hit["matched_by"] == "trigger"
    assert hit["reason"]
    miss = by_name["disk full on worker"]
    assert miss["matched"] is False
    assert "无匹配 runbook" in miss["reason"]


# ---------------------------------------------------------------------------
# l — 只读：triage 调用零 runbook 执行（执行账本文件不产生）
# ---------------------------------------------------------------------------

def test_l_triage_never_executes_runbook(triage_home, am_up, monkeypatch):
    am_up([{"alertname": "Harbor healthcheck failed", "severity": "critical"}])

    def _boom(*a, **k):
        raise AssertionError("alert_triage 不得触碰执行链")

    monkeypatch.setattr("tools.runbook_exec.execute_runbook", _boom)
    monkeypatch.setattr("tools.runbook_exec.record_execution", _boom)
    result = json.loads(pt.alert_triage())
    assert result["matched_count"] == 1
    assert not (triage_home / "runtime" / "runbook_executions.jsonl").exists()
    # 只读：连审计也只写 triage 自己的轻量账本，不动执行账本。
    assert (triage_home / "runtime" / "alert_triage.jsonl").is_file()


def test_h_no_alertmanager_config_error(triage_home, monkeypatch):
    monkeypatch.setattr(
        pt, "_prom_config",
        lambda: {"endpoint": PROM_ENDPOINT, "alertmanager": "", "vault_path": ""},
    )
    out = pt.alert_triage()
    assert "未配置 Alertmanager" in out
