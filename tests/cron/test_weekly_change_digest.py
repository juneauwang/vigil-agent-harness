"""weekly_change_digest 采集器测试（D1/D3）。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cron" / "scripts" / "weekly_change_digest.py"
FIXTURE_DB = ROOT / "tests" / "fixtures" / "weekly_digest" / "state.db"
WINDOW_ARGS = ["--since", "2026-10-20", "--until", "2026-10-27"]

EMOJI_RE = __import__("re").compile(
    "[" 
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]"
)


def load_module():
    spec = importlib.util.spec_from_file_location("weekly_change_digest_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(ROOT),
    )


def facts(*args: str, state_dir: Path) -> dict:
    result = run_cli(
        "--db", str(FIXTURE_DB), *WINDOW_ARGS, "--format", "facts",
        "--state-dir", str(state_dir), *args,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def walk_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

def test_window_is_left_closed_right_open(tmp_path):
    """恰好在 since 的会话算；恰好在 until 的会话不算。"""
    inside = run_cli(
        "--db", str(FIXTURE_DB),
        "--since", "2026-10-21T10:00:00",
        "--until", "2026-10-21T10:00:01",
        "--format", "facts", "--state-dir", str(tmp_path),
    )
    assert inside.returncode == 0, inside.stderr
    inside_facts = json.loads(inside.stdout)
    assert [s["id"] for s in inside_facts["sessions"]] == ["20261021_100000_aaaaaa"]

    outside = run_cli(
        "--db", str(FIXTURE_DB),
        "--since", "2026-10-21T09:59:59",
        "--until", "2026-10-21T10:00:00",
        "--format", "facts", "--state-dir", str(tmp_path),
    )
    assert outside.returncode == 0, outside.stderr
    assert json.loads(outside.stdout)["sessions"] == []


def test_iso_week_window_and_label(tmp_path):
    module = load_module()
    args = argparse.Namespace(window=None, week="2026-W44", since=None, until=None)
    window = module.resolve_window(args, now=datetime(2026, 10, 27, 19, 0, tzinfo=module.SHANGHAI))
    assert window == {
        "since": "2026-10-26T00:00:00+08:00",
        "until": "2026-11-02T00:00:00+08:00",
        "label": "2026-W44",
    }


def test_default_window_is_seven_days(tmp_path):
    result = run_cli(
        "--db", str(FIXTURE_DB), "--format", "facts", "--state-dir", str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    window = json.loads(result.stdout)["window"]
    since = datetime.fromisoformat(window["since"])
    until = datetime.fromisoformat(window["until"])
    assert until - since == timedelta(days=7)
    assert since.utcoffset() == timedelta(hours=8)
    assert window["label"].startswith("2026-W")


# ---------------------------------------------------------------------------
# facts 内容
# ---------------------------------------------------------------------------

def test_fixture_facts_schema_and_window_filter(tmp_path):
    data = facts(state_dir=tmp_path)

    assert set(data) == {
        "window", "generated_at", "db", "totals", "sessions",
        "unclassified", "topology", "repos", "gaps",
    }
    assert data["window"]["since"] == "2026-10-20T00:00:00+08:00"
    assert data["window"]["until"] == "2026-10-27T00:00:00+08:00"
    assert data["window"]["label"] == "2026-W43"

    # 3 个窗口内会话；窗口外的 dddddd 必须不在。
    ids = [s["id"] for s in data["sessions"]]
    assert ids == [
        "20261021_100000_aaaaaa",
        "20261023_140000_bbbbbb",
        "20261025_090000_cccccc",
    ]
    assert "20261010_120000_dddddd" not in ids
    assert data["totals"]["sessions"] == 3
    assert data["totals"]["messages"] == 17
    assert data["totals"]["write_actions"] == 2
    assert data["totals"]["failed_sessions"] == 1
    assert data["totals"]["tokens"] == {"in": 25000, "out": 5800}

    first = data["sessions"][0]
    assert first["duration_min"] == 42
    assert first["first_user_msg"] == "帮我把 nginx 配置更新一下并重启 顺便确认配置语法"
    assert [a["action"] for a in first["write_actions"]] == ["restart", "write_file"]
    assert first["write_actions"][0]["tool"] == "terminal"
    assert first["write_actions"][0]["count"] == 1
    assert first["read_only_tool_top"] == [["read_file", 1]]

    second = data["sessions"][1]
    assert second["end_reason"] == "interrupted"
    assert second["errors"] == ["command not found: frobnicate"]
    assert data["unclassified"] == [{
        "session_id": "20261023_140000_bbbbbb",
        "tool": "terminal",
        "summary": "frobnicate --upgrade-now",
    }]

    # effect_disposition 全是 NULL，仍能判出写操作。
    assert data["sessions"][0]["write_actions"]


def test_facts_strings_are_short_and_single_line(tmp_path):
    data = facts(state_dir=tmp_path)
    for text in walk_strings(data):
        assert "\n" not in text
        assert "\r" not in text
    for session in data["sessions"]:
        assert len(session["first_user_msg"]) <= 200
        for action in session["write_actions"]:
            assert len(action["summary"]) <= 120
        for error in session["errors"]:
            assert len(error) <= 160
    for item in data["unclassified"]:
        assert len(item["summary"]) <= 120


def test_zero_sessions_does_not_crash(tmp_path):
    result = run_cli(
        "--db", str(FIXTURE_DB),
        "--since", "2020-01-01", "--until", "2020-01-08",
        "--format", "facts", "--state-dir", str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sessions"] == []
    assert data["totals"] == {
        "sessions": 0, "messages": 0, "write_actions": 0,
        "failed_sessions": 0, "tokens": {"in": 0, "out": 0},
    }
    assert "数据缺：本周期无作业记录" in data["gaps"]


# ---------------------------------------------------------------------------
# 拓扑快照 diff
# ---------------------------------------------------------------------------

TOPO_V1 = """\
version: 1
updated_at: '2026-10-01'
environments:
- name: prod
  isolation: strict
hosts:
- name: harbor
  env: prod
  last_verified: '2026-09-01'
  source: agent
- name: oldbox
  env: prod
  last_verified: '2026-09-01'
  source: discovered
"""

TOPO_V2 = """\
version: 2
updated_at: '2026-10-27'
environments:
- name: prod
  isolation: strict
hosts:
- name: harbor
  env: prod
  last_verified: '2026-09-27'
  source: agent
- name: newbox
  env: prod
  last_verified: '2026-10-26'
  source: agent
"""


def _run_topo(tmp_path: Path, topo: Path) -> dict:
    result = run_cli(
        "--db", str(FIXTURE_DB), *WINDOW_ARGS, "--format", "facts",
        "--state-dir", str(tmp_path), "--topology", str(topo),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_topology_snapshot_first_run_then_diff(tmp_path):
    topology = tmp_path / "topology.yaml"
    state_dir = tmp_path / "state"
    topology.write_text(TOPO_V1, encoding="utf-8")

    first = _run_topo(state_dir, topology)["topology"]
    assert first["snapshot_missing"] is True
    assert first["changed"] == [] and first["added"] == [] and first["removed"] == []
    snapshot_path = Path(first["snapshot_path"])
    assert snapshot_path.exists()

    topology.write_text(TOPO_V2, encoding="utf-8")
    second = _run_topo(state_dir, topology)["topology"]
    assert second["snapshot_missing"] is False
    assert second["changed"] == [{
        "entity": "harbor",
        "env": "prod",
        "field": "last_verified",
        "old": "2026-09-01",
        "new": "2026-09-27",
        "source": "agent",
    }]
    assert second["added"] == [{"entity": "newbox", "env": "prod", "source": "agent"}]
    assert second["removed"] == [{"entity": "oldbox", "env": "prod", "source": "discovered"}]

    third = _run_topo(state_dir, topology)["topology"]
    assert third["changed"] == [] and third["added"] == [] and third["removed"] == []


def test_missing_topology_is_a_gap_not_a_crash(tmp_path):
    data = facts(state_dir=tmp_path)
    assert data["topology"]["snapshot_missing"] is True
    assert data["topology"]["changed"] == []
    assert any("topology.yaml 不存在" in gap for gap in data["gaps"])


# ---------------------------------------------------------------------------
# git 只读采集
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, env=None) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


def test_repos_collect_commits_and_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-10-22T10:00:00+08:00",
        "GIT_COMMITTER_DATE": "2026-10-22T10:00:00+08:00",
    }
    _git(repo, "add", "a.txt", env=env)
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
         "commit", "-q", "-m", "feat: demo change", env=env)

    data = facts("--repos", str(repo), state_dir=tmp_path / "state")
    assert len(data["repos"]) == 1
    entry = data["repos"][0]
    assert entry["path"] == str(repo.resolve())
    assert entry["dirty"] is False
    assert len(entry["commits"]) == 1
    commit = entry["commits"][0]
    assert len(commit["sha"]) == 7
    assert commit["date"] == "2026-10-22"
    assert commit["author"] == "Fixture"
    assert commit["subject"] == "feat: demo change"

    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    dirty = facts("--repos", str(repo), state_dir=tmp_path / "state2")
    assert dirty["repos"][0]["dirty"] is True


def test_non_repo_path_is_a_gap(tmp_path):
    data = facts("--repos", str(tmp_path), state_dir=tmp_path / "state")
    assert data["repos"][0]["commits"] == []
    assert any("不是 git 仓库" in gap for gap in data["gaps"])


# ---------------------------------------------------------------------------
# Markdown 契约
# ---------------------------------------------------------------------------

def test_markdown_contract(tmp_path):
    out = tmp_path / "report.md"
    result = run_cli(
        "--db", str(FIXTURE_DB), *WINDOW_ARGS, "--format", "md",
        "--state-dir", str(tmp_path / "state"), "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")

    assert text.startswith("# Vigil 周报 2026-W43（10-20 ~ 10-27）")
    headings = [
        "## 1. 摘要",
        "## 2. 变更明细",
        "## 3. 作业明细",
        "## 4. 风险与未闭环",
        "## 5. 下周期",
    ]
    positions = [text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "| 时间 | 会话 | 主要动作 | 写操作 | 结果 |" in text
    assert "20261010_120000_dddddd" not in text
    assert "本周期无拓扑变更" in text
    assert EMOJI_RE.search(text) is None
    # 表格列数 <= 6
    for line in text.splitlines():
        if line.startswith("|"):
            assert line.count("|") - 1 <= 6
    # 第 5 段至少 1 条
    tail = text.split("## 5. 下周期", 1)[1].strip().splitlines()
    assert 1 <= len(tail) <= 3


def test_markdown_topology_table(tmp_path):
    topology = tmp_path / "topology.yaml"
    state_dir = tmp_path / "state"
    out = tmp_path / "report.md"
    topology.write_text(TOPO_V1, encoding="utf-8")

    # 首跑建基线，无变更表。
    run_cli(
        "--db", str(FIXTURE_DB), *WINDOW_ARGS, "--format", "md",
        "--state-dir", str(state_dir), "--topology", str(topology), "--out", str(out),
    )
    assert "本周期无拓扑变更" in out.read_text(encoding="utf-8")

    topology.write_text(TOPO_V2, encoding="utf-8")
    result = run_cli(
        "--db", str(FIXTURE_DB), *WINDOW_ARGS, "--format", "md",
        "--state-dir", str(state_dir), "--topology", str(topology), "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")
    assert "| 环境 | 实体 | 变化 | 来源 |" in text
    assert "last_verified: 2026-09-01 -> 2026-09-27" in text
    assert "| prod | newbox | 新增 | agent |" in text
    assert "| prod | oldbox | 移除 | discovered |" in text


# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------

def test_exit_code_3_for_bad_db(tmp_path):
    result = run_cli(
        "--db", str(tmp_path / "missing.db"), "--format", "facts",
        "--state-dir", str(tmp_path),
    )
    assert result.returncode == 3
    assert "数据库不存在" in result.stderr


def test_exit_code_2_for_bad_args(tmp_path):
    bad_window = run_cli(
        "--db", str(FIXTURE_DB), "--window", "nope",
        "--format", "facts", "--state-dir", str(tmp_path),
    )
    assert bad_window.returncode == 2

    half_window = run_cli(
        "--db", str(FIXTURE_DB), "--since", "2026-10-20",
        "--format", "facts", "--state-dir", str(tmp_path),
    )
    assert half_window.returncode == 2
