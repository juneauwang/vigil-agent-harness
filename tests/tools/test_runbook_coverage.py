"""批八十一：runbook 覆盖率——确定性高危覆盖率 + 审计动作使用率（只读）。"""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def chome(tmp_path, monkeypatch):
    """隔离 home：矩阵（prod required 高危）、runbooks（v0.2 + v0.1）、轨迹事件。"""
    home = tmp_path / "vigil_home"
    (home / "runbooks").mkdir(parents=True)
    (home / "trajectory").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    from tools.matrix_data import MATRIX_FILENAME
    (home / MATRIX_FILENAME).write_text("""
schema_version: 1
matrix:
  local:
    query: execute
    restart: approve
  prod:
    restart:
      approve: required
    reboot:
      approve: required
    remove:
      approve: required
    query: execute
""", encoding="utf-8")
    (home / "runbooks" / "rb-v2.yaml").write_text("""
name: rb-v2
title: V2
version: 2
env: prod
steps:
- id: q1
  title: 查询
  action: query
  params:
    pattern: x
- id: rs
  title: 重启
  action: restart
  params:
    target: local
""", encoding="utf-8")
    (home / "runbooks" / "rb-v1.yaml").write_text("""
name: rb-v1
title: V1
version: 1
steps:
- id: d
  title: 诊断
  commands:
  - "docker ps --filter name=harbor"
  - "docker logs --tail 20 harbor"
""", encoding="utf-8")
    return home


def _write_trajectory(home, events):
    path = home / "trajectory" / "t.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ev(etype, action, ts=None):
    return {"type": etype, "action": action, "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def test_runbook_actions_v1_v2(chome):
    from tools.runbook_coverage import runbook_actions
    actions = runbook_actions(chome)
    # v0.2 action 字段
    assert "query" in actions and "rb-v2" in actions["query"]
    assert "restart" in actions and "rb-v2" in actions["restart"]
    # v0.1 commands 过 classifier：docker ps → query、docker logs → fetch_log
    assert "query" in actions and "rb-v1" in actions["query"]
    assert "fetch_log" in actions and "rb-v1" in actions["fetch_log"]


def test_high_risk_snapshot(chome):
    from tools.runbook_coverage import high_risk_actions, high_risk_snapshot
    assert high_risk_actions(chome) == ["reboot", "remove", "restart"]
    snap = high_risk_snapshot(chome)
    assert snap["total"] == 3
    assert snap["covered"] == 1  # restart 被 rb-v2 覆盖
    assert snap["uncovered"] == ["reboot", "remove"]
    assert snap["coverage_pct"] == 33


def test_empty_matrix_and_runbooks_no_crash(tmp_path, monkeypatch):
    home = tmp_path / "empty"
    (home / "runbooks").mkdir(parents=True)
    (home / "trajectory").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    from tools.runbook_coverage import coverage_snapshot
    snap = coverage_snapshot(home)
    assert snap["high_risk"]["total"] == 0
    assert snap["high_risk"]["coverage_pct"] == 0
    assert snap["usage"]["total_unique"] == 0
    assert snap["usage"]["coverage_pct"] == 0
    assert snap["usage"]["gaps"] == []


def test_usage_snapshot_counts_and_gaps(chome):
    from tools.runbook_coverage import usage_snapshot
    _write_trajectory(chome, [
        _ev("tool_call", "systemctl restart nginx"),
        _ev("tool_call", "systemctl restart nginx"),
        _ev("approval", "systemctl restart nginx"),
        _ev("tool_call", "docker ps | grep harbor"),
        _ev("tool_call", "kubectl rollout restart deploy/gateway"),
        _ev("tool_call", "reboot"),
        # unknown：classifier 识别不出 → 不计入
        _ev("tool_call", "some-unknown-tool --weird"),
        # 窗口外（30 天前）→ 不计入
        _ev("tool_call", "systemctl restart nginx",
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 40 * 86400))),
    ])
    usage = usage_snapshot(chome)
    counts = {a["action"]: a["use_count"] for a in usage["actions"]}
    # kubectl rollout restart → restart（classifier 粗粒度语义）
    assert counts == {"restart": 4, "query": 1, "reboot": 1}
    assert usage["audit_events_scanned"] == 7
    assert usage["total_unique"] == 3
    covered = {a["action"] for a in usage["actions"] if a["covered"]}
    assert covered == {"query", "restart"}
    assert usage["coverage_pct"] == 67
    assert usage["gaps"][0]["action"] == "reboot"  # 高频未覆盖 top1
    assert usage["gaps"][0]["use_count"] == 1


def test_usage_no_audit_data(chome):
    from tools.runbook_coverage import usage_snapshot
    usage = usage_snapshot(chome)
    assert usage["total_unique"] == 0
    assert usage["coverage_pct"] == 0
    assert usage["gaps"] == []
