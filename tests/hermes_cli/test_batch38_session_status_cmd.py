"""批三十八任务 5：`vigil sessions status <id>` 会话状态观测命令（§AS A5 + §AQ）。

覆盖：新 session（running）与老数据（status 缺失 → ended_at 兜底）都能输出；
end_reason/ended_at/last_activity（含 messages 最大时间戳取新）/最近工具调用
在输出里可见；未知 id 明确报错；busy 信号来自活动注册表。
"""

from __future__ import annotations

import time

import pytest

from hermes_cli.sessions_cmd import _cmd_session_status, _session_busy_signal
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    database = SessionDB()
    try:
        yield database
    finally:
        database.close()


def test_status_output_running_session_with_tool_calls(db, capsys):
    sid = db.create_session("s1", source="web")
    db.append_message(sid, role="user", content="hi", timestamp=1000.0)
    db.append_message(
        sid, role="assistant", content="", timestamp=1001.0,
        tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
    )
    db.append_message(
        sid, role="tool", content="ok: hostname", tool_name="read_file", timestamp=1002.0,
    )
    db.append_message(sid, role="assistant", content="done", timestamp=1003.0)
    db.touch_session_activity(sid, ts=900.0, description="stale heartbeat")

    _cmd_session_status(db, "s1")
    out = capsys.readouterr().out
    assert "Status:         running" in out
    assert "Session:        s1" in out
    # last_activity 取 messages 最大时间戳（1003 > 900 心跳）而非 turn 起点。
    assert "Last activity:  1970-01-01 00:16:43 UTC" in out
    assert "Recent tools:" in out
    assert "read_file" in out


def test_status_output_ended_session_with_reason(db, capsys):
    sid = db.create_session("s1", source="cli")
    db.end_session(sid, "cli_close")
    _cmd_session_status(db, "s1")
    out = capsys.readouterr().out
    assert "Status:         ended" in out
    assert "End reason:     cli_close" in out
    assert "Ended at:       " in out


def test_status_prefers_explicit_status_over_ended_at(db, capsys):
    """显式 status 优先：行带 ended_at 但被标 running（web chat 多轮会话
    turn 间形态）时按 status 展示，不按 ended_at 误报为 ended。"""
    sid = db.create_session("s1", source="cli")
    db.finalize_session_row(sid, status="ended", end_reason="turn_complete")
    db.set_session_status(sid, "running")
    _cmd_session_status(db, "s1")
    out = capsys.readouterr().out
    assert "Status:         running" in out
    assert "Ended at:       " in out


def test_status_unknown_session_prints_not_found(db, capsys):
    _cmd_session_status(db, "chat_nope")
    out = capsys.readouterr().out
    assert "Session not found: chat_nope" in out


def test_status_resolves_unique_prefix(db, capsys):
    sid = db.create_session("chat_abcd1234", source="web")
    _cmd_session_status(db, "chat_abcd")
    out = capsys.readouterr().out
    assert sid in out


def test_busy_signal_reflects_active_registry(db, monkeypatch, tmp_path):
    assert _session_busy_signal("chat_xyz") is False

    def _fake_snapshot():
        return [{"session_id": "chat_xyz"}]

    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot",
        _fake_snapshot,
    )
    # sessions_cmd 内是调用时 import —— 打 active_sessions 模块即可。
    from hermes_cli import active_sessions as _as_mod
    monkeypatch.setattr(_as_mod, "active_session_registry_snapshot", _fake_snapshot)
    assert _session_busy_signal("chat_xyz") is True
    assert _session_busy_signal("chat_other") is False
