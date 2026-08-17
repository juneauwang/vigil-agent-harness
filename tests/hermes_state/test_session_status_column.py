"""批三十八任务 1：sessions 显式 status 字段 + finalize 统一入口。

覆盖：建行默认 running；end_session/reopen/promote_to_session_reset/压缩父行
的 status 迁移；set_session_status 非法值拒绝；finalize_session_row 落终态；
finalize 自身失败 → finalize_error 兜底落库；v26 存量行迁移（ended_at 非 NULL
→ ended）；UI 状态映射 helper（新字段/老数据两态）。
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_common import (
    SESSION_STATUS_FINALIZE_ERROR,
    SESSION_STATUS_INTERRUPTED,
    SESSION_STATUS_RUNNING,
    session_status_is_running,
)


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def test_create_session_defaults_to_running(db):
    sid = db.create_session("s1", source="cli")
    row = db.get_session(sid)
    assert row["status"] == SESSION_STATUS_RUNNING


def test_end_session_sets_ended_status(db):
    sid = db.create_session("s1", source="cli")
    db.end_session(sid, "cli_close")
    row = db.get_session(sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "cli_close"
    assert row["ended_at"] is not None


def test_reopen_session_returns_to_running(db):
    sid = db.create_session("s1", source="cli")
    db.end_session(sid, "cli_close")
    db.reopen_session(sid)
    row = db.get_session(sid)
    assert row["status"] == SESSION_STATUS_RUNNING
    assert row["ended_at"] is None
    assert row["end_reason"] is None


def test_promote_to_session_reset_sets_ended_status(db):
    sid = db.create_session("s1", source="cli")
    assert db.promote_to_session_reset(sid, reason="idle") is True
    row = db.get_session(sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "idle"


def test_compression_parent_lands_ended_status(db):
    parent = db.create_session("parent", source="cli")
    db.append_message(parent, role="user", content="hi")
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id="child",
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}],
        system_prompt=None,
        source="cli",
        model=None,
        model_config=None,
        require_compression_lease=False,
    )
    row = db.get_session(parent)
    assert row["status"] == "ended"
    assert row["end_reason"] == "compression"


def test_set_session_status_rejects_unknown_value(db):
    sid = db.create_session("s1", source="cli")
    with pytest.raises(ValueError):
        db.set_session_status(sid, "bogus")


def test_finalize_session_row_stamps_terminal_state(db):
    sid = db.create_session("s1", source="cli")
    assert db.finalize_session_row(
        sid, status="ended", end_reason="approval_timeout"
    ) is True
    row = db.get_session(sid)
    assert row["status"] == "ended"
    assert row["end_reason"] == "approval_timeout"
    assert row["ended_at"] is not None


def test_finalize_session_row_interrupted(db):
    sid = db.create_session("s1", source="cli")
    db.finalize_session_row(sid, status=SESSION_STATUS_INTERRUPTED, end_reason="interrupted")
    row = db.get_session(sid)
    assert row["status"] == SESSION_STATUS_INTERRUPTED
    assert row["end_reason"] == "interrupted"


def test_finalize_session_row_preserves_existing_boundary_reason(db):
    """已存在的终态 reason（如 compression）不被后续 generic finalize 覆盖。"""
    sid = db.create_session("s1", source="cli")
    db.finalize_session_row(sid, status="ended", end_reason="compression")
    db.finalize_session_row(sid, status="ended", end_reason="turn_complete")
    row = db.get_session(sid)
    assert row["end_reason"] == "compression"


def test_finalize_failure_stamps_finalize_error(db, monkeypatch):
    """finalize 自身失败 → finalize_error 落库（不允许静默跳过）。"""
    sid = db.create_session("s1", source="cli")
    real_write = db._execute_write
    calls = []

    def flaky(fn, **kwargs):
        if not calls:
            calls.append(1)
            raise RuntimeError("db wedged")
        return real_write(fn, **kwargs)

    monkeypatch.setattr(db, "_execute_write", flaky)
    with pytest.raises(RuntimeError):
        db.finalize_session_row(sid, status="ended", end_reason="error")
    row = db.get_session(sid)
    assert row["status"] == SESSION_STATUS_FINALIZE_ERROR


def test_v26_migration_backfills_legacy_ended_rows(tmp_path):
    """存量行迁移：ended_at 非 NULL → ended，否则 running（只跑一次，不覆盖
    后续显式写入的 failed/interrupted）。"""
    path = tmp_path / "state.db"
    db = SessionDB(path)
    ended = db.create_session("ended1", source="cli")
    live = db.create_session("live1", source="cli")
    db.end_session(ended, "cli_close")
    # 模拟 v25 存量库：status 回退为列默认值 running + 版本钉在 25。
    conn = db._conn
    conn.execute("UPDATE sessions SET status = 'running' WHERE id = ?", (ended,))
    conn.execute("UPDATE schema_version SET version = 25")
    conn.commit()
    db.close()

    db2 = SessionDB(path)
    try:
        assert db2.get_session(ended)["status"] == "ended"
        assert db2.get_session(live)["status"] == SESSION_STATUS_RUNNING
        # 迁移只跑一次：显式 failed + ended_at 的行在重启后不被回写成 ended。
        failed = db2.create_session("failed1", source="cli")
        db2.finalize_session_row(failed, status="failed", end_reason="error")
        conn = db2._conn
        conn.execute("UPDATE schema_version SET version = 25")
        conn.commit()
    finally:
        db2.close()

    db3 = SessionDB(path)
    try:
        assert db3.get_session(failed)["status"] == "failed"
    finally:
        db3.close()


def test_reconcile_adds_status_column_to_legacy_db(tmp_path):
    """真·v25 库（无 status 列）：_reconcile_columns 补列 + v26 回填 ended。"""
    import hermes_state_common as hsc

    legacy_sql = hsc.SCHEMA_SQL.replace(
        "    status TEXT NOT NULL DEFAULT 'running',\n", ""
    )
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(legacy_sql)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at, end_reason) "
        "VALUES ('old1', 'cli', 0, 100, 'cli_close')"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('old2', 'cli', 0)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (25)")
    conn.commit()
    conn.close()

    db = SessionDB(path)
    try:
        assert db.get_session("old1")["status"] == "ended"
        assert db.get_session("old2")["status"] == SESSION_STATUS_RUNNING
    finally:
        db.close()


def test_ui_status_mapping_new_field_and_legacy_fallback():
    # 新字段：只有 running 算活。
    assert session_status_is_running(SESSION_STATUS_RUNNING, ended_at=None) is True
    assert session_status_is_running("ended", ended_at=123.0) is False
    assert session_status_is_running("interrupted", ended_at=123.0) is False
    assert session_status_is_running("failed", ended_at=123.0) is False
    assert session_status_is_running(SESSION_STATUS_FINALIZE_ERROR, ended_at=123.0) is False
    # 老数据（无 status）：回退 ended_at 是否 NULL。
    assert session_status_is_running(None, ended_at=None) is True
    assert session_status_is_running(None, ended_at=123.0) is False
