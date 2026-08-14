"""批次二十三 任务 2/3 — ``vigil trajectory`` 查询命令与复盘回放验收测试。

覆盖：list 列出 2 个 session、show 按 seq 排序 + --type 过滤、search 命中/
不命中、prune 保留期删除、--replay 紧凑时间线 + 事件间隔标注 + --approval
过滤。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent.trajectory as trajectory_mod
from hermes_cli.subcommands import trajectory as trajectory_cmd


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    trajectory_mod.reset_trajectory_state_for_tests()
    yield tmp_path
    trajectory_mod.reset_trajectory_state_for_tests()


def _write_fixture(tmp_path: Path) -> None:
    """构造两个 session 的事件文件（时间错开，供 span/间隔/prune 断言）。"""
    from agent.trajectory import record_event
    base = datetime.now(timezone.utc)

    def _record(session_id, seq_offset, type_, **kw):
        record_event(type=type_, session_id=session_id, **kw)
        # 时间戳手动改造成可控间隔（record_event 用当前时间，这里覆盖 ts）
        path = tmp_path / "trajectory" / f"{session_id}.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        ev = json.loads(lines[-1])
        ev["ts"] = (base + timedelta(seconds=seq_offset)).isoformat(timespec="milliseconds")
        lines[-1] = json.dumps(ev, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # sess-prod-1：4 条，间隔 2.5s / 3.0s / 1.0s
    _record("sess-prod-1", 0.0, "tool_call", tool="terminal", action="kubectl get pods -n prod")
    _record("sess-prod-1", 2.5, "tool_result", tool="terminal", action="kubectl get pods -n prod", result="exit=0 ready")
    _record("sess-prod-1", 5.5, "approval", approval="requested", action="sudo systemctl restart hermes-gateway")
    _record("sess-prod-1", 6.5, "approval", approval="approved", action="sudo systemctl restart hermes-gateway", meta={"scope": "once"})
    # sess-audit-2：2 条
    _record("sess-audit-2", 8.0, "tool_call", tool="terminal", action="tail -n 100 /var/log/nginx/error.log")
    _record("sess-audit-2", 9.5, "tool_result", tool="terminal", action="tail -n 100 /var/log/nginx/error.log", result="exit=0 2026/08/14 22:00:00 [error]")


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _run(sub, **kw) -> int:
    kw.setdefault("event_type", None)
    kw.setdefault("replay", False)
    kw.setdefault("approval", False)
    return trajectory_cmd.run(_Args(trajectory_command=sub, **kw))


# ---------------------------------------------------------------------------
# list / show / search / prune
# ---------------------------------------------------------------------------

def test_list_shows_two_sessions(tmp_path, capsys):
    _write_fixture(tmp_path)
    assert _run("list") == 0
    out = capsys.readouterr().out
    assert "sess-prod-1" in out
    assert "sess-audit-2" in out
    # 事件数正确
    assert "4" in out.split("sess-prod-1")[1].splitlines()[0]
    assert "2" in out.split("sess-audit-2")[1].splitlines()[0]


def test_show_orders_by_seq_and_filters_by_type(tmp_path, capsys):
    _write_fixture(tmp_path)
    assert _run("show", session_id="sess-prod-1") == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 4
    # 时间正序（seq 排序）
    assert "[tool_call]" in lines[0]
    assert "[tool_result]" in lines[1]
    assert "kubectl get pods -n prod" in lines[1]

    # --type terminal：只看命令事件（type 或 tool 命中）
    capsys.readouterr()
    assert _run("show", session_id="sess-prod-1", event_type="terminal",
                replay=False, approval=False) == 0
    out = capsys.readouterr().out
    assert "[approval]" not in out
    assert "[tool_call]" in out and "[tool_result]" in out

    # --type approval：只看审批
    capsys.readouterr()
    assert _run("show", session_id="sess-prod-1", event_type="approval",
                replay=False, approval=False) == 0
    out = capsys.readouterr().out
    assert "[tool_call]" not in out
    assert out.count("[approval]") == 2


def test_show_missing_session(tmp_path, capsys):
    _write_fixture(tmp_path)
    assert _run("show", session_id="nope", event_type=None,
                replay=False, approval=False) == 1
    assert "No trajectory found" in capsys.readouterr().err


def test_search_hits_and_misses(tmp_path, capsys):
    _write_fixture(tmp_path)
    assert _run("search", query="kubectl") == 0
    out = capsys.readouterr().out
    assert "sess-prod-1" in out and "kubectl" in out
    assert "nginx" not in out

    capsys.readouterr()
    assert _run("search", query="ghost-query") == 0
    assert "No trajectory events match" in capsys.readouterr().out


def test_prune_removes_old_sessions(tmp_path, capsys):
    _write_fixture(tmp_path)
    before = (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat()
    assert _run("prune", before=before) == 0
    out = capsys.readouterr().out
    assert "Pruned 2" in out
    assert not (tmp_path / "trajectory" / "sess-prod-1.jsonl").exists()

    # 全部删完后再 prune 无文件可删
    capsys.readouterr()
    assert _run("prune", before=before) == 0
    assert "Pruned 0" in capsys.readouterr().out


def test_prune_keeps_recent_sessions(tmp_path, capsys):
    _write_fixture(tmp_path)
    before = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
    assert _run("prune", before=before) == 0
    assert "Pruned 0" in capsys.readouterr().out
    assert (tmp_path / "trajectory" / "sess-prod-1.jsonl").exists()


# ---------------------------------------------------------------------------
# --replay 时间线（任务 3）
# ---------------------------------------------------------------------------

def test_replay_compact_timeline_with_deltas(tmp_path, capsys):
    _write_fixture(tmp_path)
    assert _run("show", session_id="sess-prod-1", event_type=None,
                replay=True, approval=False) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 4
    # 单行紧凑：时间 + 间隔 + 类型 + action/result
    assert lines[0].startswith("[")
    assert "+2.5s" in lines[1]
    assert "+3.0s" in lines[2]
    assert "+1.0s" in lines[3]
    assert "kubectl get pods -n prod" in lines[1]
    assert "→ exit=0 ready" in lines[1]


def test_replay_approval_filter(tmp_path, capsys):
    _write_fixture(tmp_path)
    assert _run("show", session_id="sess-prod-1", event_type=None,
                replay=True, approval=True) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all("[approval]" in ln for ln in lines)
    assert "sudo systemctl restart hermes-gateway" in lines[1]
