"""OPS-DELTA 批次三十二 — ``vigil security scrub`` 已落库凭据明文清理。

覆盖：dry-run 只报告不改写；实际清理打码 messages 文本列并重建 FTS；非敏感
内容不动；--values 追加值；--session 过滤。
"""

from __future__ import annotations

import json

import pytest

from agent import redact
from hermes_cli.scrub_secrets import cmd_scrub_secrets
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    import hermes_cli.config as hc
    hc._LOAD_CONFIG_CACHE.clear()
    redact._reset_registered_credential_values_for_tests()
    yield
    redact._reset_registered_credential_values_for_tests()


def _seed_db(secret_value: str) -> SessionDB:
    redact.register_credential_value(secret_value)
    db = SessionDB()
    db.create_session("sess-1", source="test")
    clarify_json = json.dumps({
        "question": "请输入 sudo 密码",
        "choices_offered": None,
        "user_response": secret_value,
    }, ensure_ascii=False)
    db.append_message("sess-1", "user", "帮我停一下 gitlab")
    db.append_message("sess-1", "tool", clarify_json, tool_call_id="call_1")
    db.close()
    return db


class _Args:
    dry_run = False
    session = None
    values = []


def _tool_contents() -> list:
    db = SessionDB()
    try:
        return [
            r[0] for r in db._conn.execute(
                "SELECT content FROM messages WHERE role='tool'"
            ).fetchall()
        ]
    finally:
        db.close()


def test_dry_run_reports_without_writing(tmp_path):
    _seed_db("wwplove815")
    _Args.dry_run = True
    rc = cmd_scrub_secrets(_Args)
    assert rc == 0
    assert "wwplove815" in _tool_contents()[0]  # 未改写


def test_scrub_masks_registered_value(tmp_path):
    _seed_db("wwplove815")
    _Args.dry_run = False
    rc = cmd_scrub_secrets(_Args)
    assert rc == 0
    content = _tool_contents()[0]
    assert "wwplove815" not in content
    assert "«redacted-value»" in content


def test_scrub_preserves_non_sensitive_content(tmp_path):
    _seed_db("wwplove815")
    _Args.dry_run = False
    cmd_scrub_secrets(_Args)
    db = SessionDB()
    try:
        rows = db._conn.execute(
            "SELECT content FROM messages WHERE role='user'"
        ).fetchall()
        assert "帮我停一下 gitlab" in rows[0][0]
    finally:
        db.close()


def test_scrub_extra_values(tmp_path):
    _seed_db("zz-secret-1")
    _Args.values = ["extra-secret-2"]
    db = SessionDB()
    db.append_message("sess-1", "tool", json.dumps({
        "question": "x", "user_response": "extra-secret-2",
    }), tool_call_id="call_2")
    db.close()
    cmd_scrub_secrets(_Args)
    assert "extra-secret-2" not in _tool_contents()[1]


def test_scrub_session_filter(tmp_path):
    _seed_db("wwplove815")
    db = SessionDB()
    db.create_session("other-sess", source="test")
    db.append_message("other-sess", "tool", json.dumps({
        "question": "请输入 sudo 密码", "user_response": "wwplove815",
    }), tool_call_id="call_9")
    db.close()
    _Args.session = "sess-1"
    cmd_scrub_secrets(_Args)
    # sess-1 已清理；other-sess 未动
    contents = _tool_contents()
    assert "wwplove815" not in contents[0]
    assert "wwplove815" in contents[1]


def test_scrub_no_registry_values_reports_empty(tmp_path):
    _Args.values = []
    rc = cmd_scrub_secrets(_Args)
    assert rc == 0
