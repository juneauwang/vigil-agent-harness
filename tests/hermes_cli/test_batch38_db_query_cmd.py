"""批三十八任务 5：`vigil db query` 内置只读 SQLite 查询入口（§AQ / §AS D2）。

覆盖：SELECT 可查且输出单元格过 redact；写语句（UPDATE/INSERT/DELETE/DROP/
ALTER/REPLACE/VACUUM）在打开连接前拒绝；多语句（分号拼接）拒绝；空语句拒绝；
PRAGMA/EXPLAIN 放行；DB 不存在时给出明确提示。
"""

from __future__ import annotations

import pytest

from hermes_cli.db_cmd import _cmd_db_query, _reject_write


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    from hermes_state import SessionDB
    database = SessionDB()
    try:
        yield database
    finally:
        database.close()


def test_reject_write_statements():
    for sql in [
        "UPDATE sessions SET x = 1",
        "INSERT INTO sessions (id, source, started_at) VALUES ('x', 'cli', 0)",
        "DELETE FROM sessions",
        "DROP TABLE sessions",
        "ALTER TABLE sessions ADD COLUMN x",
        "REPLACE INTO sessions (id) VALUES ('x')",
        "VACUUM",
    ]:
        assert _reject_write(sql) is not None, sql


def test_reject_multiple_statements():
    assert _reject_write("SELECT 1; DROP TABLE sessions") is not None
    assert _reject_write("SELECT 1; SELECT 2") is not None


def test_reject_empty_statement():
    assert _reject_write("   ") is not None
    assert _reject_write("") is not None


def test_allow_read_statements():
    assert _reject_write("SELECT * FROM sessions") is None
    assert _reject_write("SELECT 1") is None
    assert _reject_write("PRAGMA table_info(sessions)") is None
    assert _reject_write("EXPLAIN SELECT 1") is None
    # 大小写/首尾空白/尾分号都容忍。
    assert _reject_write("  select 1  ") is None
    assert _reject_write("SELECT 1;") is None


def test_query_prints_rows(db, capsys):
    sid = db.create_session("s1", source="web")
    db.set_session_status(sid, "ended", end_reason="approval_timeout")
    _cmd_db_query("SELECT id, status, end_reason FROM sessions")
    out = capsys.readouterr().out
    assert "s1 | ended | approval_timeout" in out


def test_query_redacts_credential_shaped_cells(db, capsys):
    _cmd_db_query(
        "SELECT 'sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF' AS secret"
    )
    out = capsys.readouterr().out
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890ABCDEF" not in out


def test_query_no_rows_prints_placeholder(db, capsys):
    _cmd_db_query("SELECT * FROM sessions WHERE id = 'chat_nope'")
    assert "(no rows)" in capsys.readouterr().out


def test_query_write_rejected_before_connection(capsys):
    with pytest.raises(SystemExit) as exc:
        _cmd_db_query("DELETE FROM sessions")
    assert exc.value.code == 2
    out = capsys.readouterr().err
    assert "read-only" in out


def test_query_missing_db_prints_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    from hermes_state import DEFAULT_DB_PATH

    assert not DEFAULT_DB_PATH.exists()
    _cmd_db_query("SELECT 1")
    assert "No session database" in capsys.readouterr().out
