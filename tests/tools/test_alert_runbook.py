"""batch87 任务 1/4 —— 告警→runbook 匹配器 + triage 轻量审计（OPS-DELTA #103）。

覆盖验收：a alertname 文本命中触发词 → high/trigger；b labels/annotations.summary
文本命中触发词；c 无触发词但 fuzzy 高分 → medium/fuzzy + alternatives；d 完全不
相关 → matched=false + 提示；e 0 runbook / 目录不存在 / 单个坏文件 → 优雅降级；
结构化触发词（v0.2 {alertname, severity}）逐字段精确匹配；trigger 命中优先于
fuzzy 高分；匹配结果字段透明（matched_by/matched_keyword）。任务 4：m 每次
triage 调用落一条 JSONL 审计（含匹配方式/是否命中），记录失败不阻断；n 路径稳定
+ recent 可读（新→旧）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.config as hc
from tools import alert_runbook as ar

HARBOR = """\
name: harbor-restart
title: Harbor 服务异常恢复
kind: incident
env: prod
summary: harbor 健康检查失败时的标准恢复流程：诊断 → 重启 → 验证。
triggers:
  - harbor healthcheck failed
  - harbor down
steps:
  - id: diagnose
    title: 诊断
    commands: ["docker ps --filter name=harbor"]
  - id: restart
    title: 重启
    commands: ["docker restart harbor"]
  - id: verify
    title: 验证
    commands: ["curl -s http://localhost/api/v2.0/health"]
"""

ARGOCD = """\
name: argocd-server-check-restart
title: argocd-server 状态探查与恢复
kind: incident
env: prod
summary: 探查 argocd-server 状态；未运行时恢复。
triggers:
  - argocd-server 未运行
  - {alertname: ArgoCDServerDown, severity: critical}
steps:
  - id: check
    title: 探查
    action: fetch_log
    params: {target: argocd, lines: 20}
"""

HARBOR_ALT = """\
name: registry-mirror-check
title: 镜像仓库不可用检查（备选 SOP）
kind: incident
env: prod
summary: 镜像仓库不可用时的探查流程（harbor/registry 拉取兜底）。
triggers:
  - registry unreachable
steps:
  - id: check
    title: 探查
    action: query
    params: {pattern: registry}
"""


@pytest.fixture
def mem_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "runbooks").mkdir(parents=True)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    hc._LOAD_CONFIG_CACHE.clear()
    yield home
    hc._LOAD_CONFIG_CACHE.clear()


def _write(home: Path, name: str, body: str) -> None:
    (home / "runbooks" / name).write_text(body, encoding="utf-8")


def _alert(**kw) -> dict:
    base = {"alertname": "", "severity": "info", "labels": {},
            "summary": "", "instance": "", "startsAt": "", "state": "active"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# a — alertname 文本命中触发词 → high/trigger
# ---------------------------------------------------------------------------

def test_a_alertname_text_hits_trigger_high(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="Harbor healthcheck failed", severity="critical"),
        home=mem_home,
    )
    assert disp["matched"] is True
    assert disp["runbook"] == "harbor-restart"
    assert disp["confidence"] == "high"
    assert disp["matched_by"] == "trigger"
    assert disp["matched_keyword"] == "harbor healthcheck failed"
    assert disp["title"] == "Harbor 服务异常恢复"
    assert disp["kind"] == "incident"
    assert disp["env"] == "prod"
    assert disp["step_count"] == 3
    assert "触发词" in disp["reason"]


# ---------------------------------------------------------------------------
# b — labels / annotations.summary 文本命中触发词
# ---------------------------------------------------------------------------

def test_b_annotation_summary_hits_trigger(mem_home):
    _write(mem_home, "argocd-server-check-restart.yaml", ARGOCD)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="ArgoCDReplicaLow", severity="warning",
               summary="argocd-server 未运行，请检查"),
        home=mem_home,
    )
    assert disp["matched"] is True
    assert disp["runbook"] == "argocd-server-check-restart"
    assert disp["confidence"] == "high"
    assert disp["matched_by"] == "trigger"
    assert disp["matched_keyword"] == "argocd-server 未运行"


def test_b_label_value_hits_trigger(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="UnknownAlert", labels={"reason": "harbor down reported"}),
        home=mem_home,
    )
    assert disp["matched"] is True
    assert disp["runbook"] == "harbor-restart"
    assert disp["matched_keyword"] == "harbor down"


def test_structured_trigger_exact_fields(mem_home):
    """v0.2 结构化触发词 {alertname, severity} 逐字段精确匹配。"""
    _write(mem_home, "argocd-server-check-restart.yaml", ARGOCD)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="ArgoCDServerDown", severity="critical"),
        home=mem_home,
    )
    assert disp["matched"] is True
    assert disp["runbook"] == "argocd-server-check-restart"
    assert disp["confidence"] == "high"
    assert disp["matched_by"] == "trigger"
    assert "alertname=ArgoCDServerDown" in disp["matched_keyword"]
    # alertname 不匹配 → 结构化不命中（AND 语义），fuzzy 也不撞（repr 无此名）
    disp2 = ar.match_alert_to_runbook(
        _alert(alertname="SomeOtherAlertDown", severity="critical"),
        home=mem_home,
    )
    assert disp2["matched"] is False


# ---------------------------------------------------------------------------
# c — 无 trigger 命中但 fuzzy 高分 → medium/fuzzy + alternatives
# ---------------------------------------------------------------------------

def test_c_fuzzy_high_medium_with_alternatives(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    _write(mem_home, "registry-mirror-check.yaml", HARBOR_ALT)
    # alertname/summary 都不含 harbor-restart 的触发词；summary 与备选 runbook
    # 的 summary 子串级模糊匹配（镜像仓库不可用）→ fuzzy 高分。
    disp = ar.match_alert_to_runbook(
        _alert(alertname="镜像仓库不可用", severity="warning"),
        home=mem_home,
    )
    assert disp["matched"] is True
    assert disp["confidence"] == "medium"
    assert disp["matched_by"] == "fuzzy"
    assert disp["runbook"] == "registry-mirror-check"
    assert disp["matched_keyword"] is None
    assert "模糊匹配" in disp["reason"]


def test_fuzzy_alternatives_limited_and_ranked(mem_home):
    """fuzzy 高分带次优（≤3、排除主命中、按评分降序）。"""
    for name, body in (
        ("registry-mirror-check.yaml", HARBOR_ALT),  # summary 含 镜像仓库不可用
        ("b-check.yaml", HARBOR_ALT.replace("registry-mirror-check", "b-check")
         .replace("镜像仓库不可用时的探查流程（harbor/registry 拉取兜底）。",
                  "镜像仓库不可用时先查 harbor 侧状态")),
    ):
        _write(mem_home, name, body)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="镜像仓库不可用"),
        home=mem_home,
    )
    assert disp["matched"] is True
    assert disp["matched_by"] == "fuzzy"
    assert disp["runbook"] == "registry-mirror-check"
    alts = disp["alternatives"]
    assert 1 <= len(alts) <= 3
    assert all(a["name"] != disp["runbook"] for a in alts)
    assert alts[0]["name"] == "b-check"


# ---------------------------------------------------------------------------
# d — 完全不相关 → matched=false + 提示
# ---------------------------------------------------------------------------

def test_d_unrelated_alert_no_match(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="disk full on worker", severity="warning",
               instance="worker-1"),
        home=mem_home,
    )
    assert disp["matched"] is False
    assert "无匹配 runbook" in disp["hint"]
    assert "runbook_create" in disp["hint"]


# ---------------------------------------------------------------------------
# e — 容错：0 runbook / 目录不存在 / 单个坏文件跳过
# ---------------------------------------------------------------------------

def test_e_no_runbooks_dir_graceful(mem_home):
    (mem_home / "runbooks").rmdir()
    disp = ar.match_alert_to_runbook(
        _alert(alertname="Harbor healthcheck failed"), home=mem_home)
    assert disp["matched"] is False
    assert "无可用 runbook" in disp["hint"]


def test_e_empty_runbooks_graceful(mem_home):
    disp = ar.match_alert_to_runbook(
        _alert(alertname="Harbor healthcheck failed"), home=mem_home)
    assert disp["matched"] is False
    assert "无可用 runbook" in disp["hint"]


def test_e_bad_file_skipped_good_file_still_matches(mem_home):
    _write(mem_home, "broken.yaml", "name: [unclosed\n  - nope\n")
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    disp = ar.match_alert_to_runbook(
        _alert(alertname="Harbor healthcheck failed"), home=mem_home)
    assert disp["matched"] is True
    assert disp["runbook"] == "harbor-restart"


# ---------------------------------------------------------------------------
# 融合优先级：trigger 命中 > fuzzy 高分
# ---------------------------------------------------------------------------

def test_trigger_beat_fuzzy_perfect_title(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    # 无触发词但 name 与告警全等 → fuzzy 100；trigger 命中必须仍优先。
    _write(mem_home, "harbor healthcheck failed.yaml".replace(" ", "-"), """\
name: harbor-healthcheck-failed
title: Harbor healthcheck failed
kind: incident
env: dev
steps: [{id: s1, title: x, commands: [a]}]
""")
    disp = ar.match_alert_to_runbook(
        _alert(alertname="Harbor healthcheck failed"), home=mem_home)
    assert disp["matched"] is True
    assert disp["runbook"] == "harbor-restart"
    assert disp["confidence"] == "high"
    assert disp["matched_by"] == "trigger"


# ---------------------------------------------------------------------------
# triage 全景（h 基础：含无匹配告警的处理）
# ---------------------------------------------------------------------------

def test_triage_panorama_includes_unmatched(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    res = ar.triage_alerts([
        _alert(alertname="Harbor healthcheck failed", severity="critical"),
        _alert(alertname="disk full on worker", severity="warning"),
    ], home=mem_home)
    assert res["count"] == 2
    assert res["matched_count"] == 1
    assert res["unmatched_count"] == 1
    by_name = {a["alertname"]: a for a in res["alerts"]}
    assert by_name["Harbor healthcheck failed"]["disposition"]["runbook"] == "harbor-restart"
    assert by_name["disk full on worker"]["disposition"]["matched"] is False
    assert "无匹配 runbook" in by_name["disk full on worker"]["disposition"]["hint"]


# ---------------------------------------------------------------------------
# 任务 4：审计（m/n）
# ---------------------------------------------------------------------------

def test_audit_one_row_per_triage_call(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    ar.triage_alerts([
        _alert(alertname="Harbor healthcheck failed"),
        _alert(alertname="disk full on worker"),
    ], home=mem_home)
    ar.triage_alerts([_alert(alertname="Harbor down")], home=mem_home)
    path = ar.alert_triage_audit_path(mem_home)
    assert path == mem_home / "runtime" / "alert_triage.jsonl"   # n：路径稳定
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 2                                        # 每次调用一条
    first = rows[0]
    assert first["type"] == "alert_triage"
    assert first["alert_count"] == 2
    assert first["matched_count"] == 1
    entries = {e["alertname"]: e for e in first["entries"]}
    assert entries["Harbor healthcheck failed"]["matched"] is True
    assert entries["Harbor healthcheck failed"]["runbook"] == "harbor-restart"
    assert entries["Harbor healthcheck failed"]["matched_by"] == "trigger"
    assert entries["disk full on worker"]["matched"] is False
    # recent：新→旧
    recent = ar.recent_alert_triage(mem_home)
    assert len(recent) == 2
    assert recent[0]["alert_count"] == 1      # 新→旧：第二条调用（1 条告警）在前
    assert recent[0]["matched_count"] == 1


def test_audit_best_effort_failure_does_not_block(mem_home):
    _write(mem_home, "harbor-restart.yaml", HARBOR)
    # 让 runtime 成为文件 → mkdir 失败 → record 静默降级，triage 照常返回。
    (mem_home / "runtime").write_text("not-a-dir", encoding="utf-8")
    res = ar.triage_alerts([_alert(alertname="Harbor healthcheck failed")], home=mem_home)
    assert res["count"] == 1
    assert res["matched_count"] == 1
