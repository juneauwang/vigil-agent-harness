"""批次二十三 任务 1 — 事件级运行轨迹记录验收测试。

覆盖：record_event 落盘（字段/seq/append-only）、action/result 强制 redact
（凭据值不落轨迹）、terminal 挂载点（真实执行产生 tool_call/tool_result）、
审批挂载点（requested/approved/denied/timeout）、单 session 容量截断、
现有 save_trajectory 回归。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent.trajectory as trajectory_mod


@pytest.fixture(autouse=True)
def _isolate_trajectory(tmp_path, monkeypatch):
    """每个用例独立 VIGIL_HOME + 清空进程内轨迹状态。"""
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    trajectory_mod.reset_trajectory_state_for_tests()
    yield tmp_path
    trajectory_mod.reset_trajectory_state_for_tests()


def _read_events(session_id: str, tmp_path: Path) -> list[dict]:
    path = tmp_path / "trajectory" / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# record_event 核心
# ---------------------------------------------------------------------------

def test_record_event_writes_append_only_jsonl(tmp_path):
    """事件落盘：字段完整、seq 递增、append-only 不覆盖。"""
    trajectory_mod.record_event(
        type="tool_call", tool="terminal", action="kubectl get pods",
        session_id="s1",
    )
    trajectory_mod.record_event(
        type="tool_result", tool="terminal", action="kubectl get pods",
        result="exit=0 ready", session_id="s1",
    )
    events = _read_events("s1", tmp_path)
    assert len(events) == 2
    assert [e["seq"] for e in events] == [1, 2]
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    for e in events:
        assert e["ts"]
        assert e["session_id"] == "s1"
        assert e["action"] == "kubectl get pods"
    assert events[1]["result"] == "exit=0 ready"


def test_record_event_redacts_secrets_in_action_and_result(tmp_path):
    """凭据值绝不落轨迹：sudo 密码注入打码，URL userinfo 打码。"""
    trajectory_mod.record_event(
        type="tool_call", tool="terminal",
        action="sudo -S <<< 'hunter2secret' apt install -y curl",
        session_id="s-secret",
    )
    trajectory_mod.record_event(
        type="tool_result", tool="terminal",
        action="curl -u admin:topsecret https://example.com/api",
        result="exit=0 hello", session_id="s-secret",
    )
    raw = (tmp_path / "trajectory" / "s-secret.jsonl").read_text(encoding="utf-8")
    assert "hunter2secret" not in raw
    assert "topsecret" not in raw
    assert "sudo -S <<< '***'" in raw
    assert "admin:***" in raw


def test_record_event_truncates_at_session_limit(tmp_path, monkeypatch):
    """超上限：记一条 trajectory_truncated 后停止记录。"""
    monkeypatch.setattr(trajectory_mod, "MAX_EVENTS_PER_SESSION", 3)
    for i in range(5):
        trajectory_mod.record_event(
            type="tool_call", tool="terminal", action=f"cmd {i}",
            session_id="s-big",
        )
    events = _read_events("s-big", tmp_path)
    assert len(events) == 4  # 3 条正常 + 1 条 truncation
    assert events[-1]["type"] == "trajectory_truncated"
    # 截断后不再记录
    trajectory_mod.record_event(
        type="tool_call", tool="terminal", action="cmd 99", session_id="s-big",
    )
    assert len(_read_events("s-big", tmp_path)) == 4


def test_record_event_never_raises(tmp_path, monkeypatch):
    """best-effort：写入失败不抛异常（目录不可写时静默降级）。"""
    monkeypatch.setattr(
        trajectory_mod, "get_trajectory_dir",
        lambda: tmp_path / "no" / "such" / "dir",
    )
    # 不抛即通过
    trajectory_mod.record_event(type="tool_call", tool="terminal", action="x", session_id="s")


# ---------------------------------------------------------------------------
# save_trajectory 回归（既有 conversation dump 通道不受影响）
# ---------------------------------------------------------------------------

def test_save_trajectory_regression(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    trajectory_mod.save_trajectory(
        [{"role": "user", "content": "hi"}], model="m", completed=True,
        filename=str(tmp_path / "trajectory_samples.jsonl"),
    )
    assert (tmp_path / "trajectory_samples.jsonl").is_file()
    entry = json.loads((tmp_path / "trajectory_samples.jsonl").read_text(encoding="utf-8"))
    assert entry["completed"] is True
    assert entry["conversations"][0]["content"] == "hi"


# ---------------------------------------------------------------------------
# terminal 挂载点（真实执行路径）
# ---------------------------------------------------------------------------

def test_terminal_tool_emits_call_and_result_events(tmp_path):
    """真实执行 terminal 命令 → tool_call + tool_result 事件落盘。"""
    from tools.terminal_tool import terminal_tool

    out = terminal_tool(command="echo trajectory-e2e-ok", session_id="s-term")
    assert "trajectory-e2e-ok" in out
    events = _read_events("s-term", tmp_path)
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[0]["tool"] == "terminal"
    assert events[0]["action"] == "echo trajectory-e2e-ok"
    assert events[1]["result"].startswith("exit=0")


def test_terminal_tool_redacts_secret_in_trajectory(tmp_path):
    """命令含密钥 → 轨迹里 action/result 打码（head/tail mask 形态）。

    不用 sudo -S（会被 sudo stdin guard 硬拒，命令不执行也就无事件）——
    用 echo 一个 GitHub token 形态的串，命令真实执行且轨迹打码。
    """
    from tools.terminal_tool import terminal_tool

    terminal_tool(
        command="echo ghp_abcdefghijklmnopqrstuvwxyz123456",
        session_id="s-term-secret",
    )
    raw = (tmp_path / "trajectory" / "s-term-secret.jsonl").read_text(encoding="utf-8")
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in raw
    assert "ghp_ab...3456" in raw


def test_terminal_append_only_two_calls_two_pairs(tmp_path):
    """两次执行各记一对事件，append-only 不覆盖。"""
    from tools.terminal_tool import terminal_tool

    terminal_tool(command="echo one", session_id="s-append")
    terminal_tool(command="echo two", session_id="s-append")
    events = _read_events("s-append", tmp_path)
    assert [e["type"] for e in events] == [
        "tool_call", "tool_result", "tool_call", "tool_result",
    ]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# approval 挂载点（真实审批路径）
# ---------------------------------------------------------------------------

def test_approval_denied_emits_requested_and_denied(tmp_path, monkeypatch):
    """CLI 审批 deny → requested + denied 事件。"""
    import tools.approval as approval_mod

    monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
    monkeypatch.setattr(
        approval_mod, "prompt_dangerous_approval",
        lambda *a, **kw: "deny",
    )
    result = approval_mod.check_dangerous_command(
        "rm -rf /tmp/trajectory-approval-test", env_type="local",
    )
    assert result["approved"] is False
    events = _read_events("default", tmp_path)
    approvals = [e for e in events if e.get("type") == "approval"]
    assert [e["approval"] for e in approvals] == ["requested", "denied"]


def test_approval_timeout_emits_timeout(tmp_path, monkeypatch):
    import tools.approval as approval_mod

    monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
    monkeypatch.setattr(
        approval_mod, "prompt_dangerous_approval",
        lambda *a, **kw: "timeout",
    )
    result = approval_mod.check_dangerous_command(
        "rm -rf /tmp/trajectory-approval-test", env_type="local",
    )
    assert result.get("outcome") == "timeout"
    approvals = [
        e for e in _read_events("default", tmp_path)
        if e.get("type") == "approval"
    ]
    assert [e["approval"] for e in approvals] == ["requested", "timeout"]


def test_approval_approved_emits_approved(tmp_path, monkeypatch):
    import tools.approval as approval_mod

    monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
    monkeypatch.setattr(
        approval_mod, "prompt_dangerous_approval",
        lambda *a, **kw: "once",
    )
    result = approval_mod.check_dangerous_command(
        "rm -rf /tmp/trajectory-approval-test", env_type="local",
    )
    assert result["approved"] is True
    approvals = [
        e for e in _read_events("default", tmp_path)
        if e.get("type") == "approval"
    ]
    assert [e["approval"] for e in approvals] == ["requested", "approved"]
    assert approvals[-1]["action"] == "rm -rf /tmp/trajectory-approval-test"
