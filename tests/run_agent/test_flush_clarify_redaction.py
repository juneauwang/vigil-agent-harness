"""OPS-DELTA 批次三十二 — clarify 敏感答复落 state.db 前打码（_flush_messages_to_session_db）。

含敏感关键词的 clarify 工具结果（question+user_response JSON）写 SQLite 前过
redact——答复值已登记进凭据值登记表 → 精确打码；live 内存消息保持明文（agent
仍需该值去 credential_vault.store），只打码落库副本。非敏感答复不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from agent import redact
from hermes_state import SessionDB
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    import hermes_cli.config as hc
    hc._LOAD_CONFIG_CACHE.clear()
    redact._reset_registered_credential_values_for_tests()
    yield
    redact._reset_registered_credential_values_for_tests()


def _make_agent(db_path: Path, session_id: str) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", db_path.parent),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = None
    db = SessionDB(db_path=db_path)
    db.create_session(session_id=session_id, source="test")
    agent._session_db = db
    agent._session_db_created = True
    agent.session_id = session_id
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    return agent


def _durable_contents(db_path: Path, session_id: str) -> list[str]:
    db = SessionDB(db_path=db_path)
    try:
        return [
            row["content"] or ""
            for row in db.get_messages_as_conversation(session_id)
        ]
    finally:
        db.close()


def test_sensitive_clarify_result_masked_at_flush(tmp_path):
    redact.register_credential_value("wwplove815")
    agent = _make_agent(tmp_path / "state.db", "sess-1")

    clarify_json = json.dumps({
        "question": "请输入 sudo 密码",
        "choices_offered": None,
        "user_response": "wwplove815",
    }, ensure_ascii=False)
    live_msg = {"role": "tool", "name": "clarify",
                "tool_call_id": "call_1", "content": clarify_json}
    agent._flush_messages_to_session_db([live_msg])

    contents = _durable_contents(tmp_path / "state.db", "sess-1")
    assert any("wwplove815" not in c and "«redacted-value»" in c for c in contents)
    # live 内存消息保持明文
    assert live_msg["content"] == clarify_json


def test_non_sensitive_clarify_result_unchanged(tmp_path):
    agent = _make_agent(tmp_path / "state.db", "sess-1")
    msg = {"role": "tool", "name": "clarify", "tool_call_id": "call_2",
           "content": '{"question": "How are you?", "user_response": "fine"}'}
    agent._flush_messages_to_session_db([msg])
    contents = _durable_contents(tmp_path / "state.db", "sess-1")
    assert any('"fine"' in c for c in contents)


def test_non_clarify_tool_result_unchanged(tmp_path):
    redact.register_credential_value("wwplove815")
    agent = _make_agent(tmp_path / "state.db", "sess-1")
    msg = {"role": "tool", "name": "web_search", "tool_call_id": "call_3",
           "content": "results for wwplove815 search"}
    agent._flush_messages_to_session_db([msg])
    contents = _durable_contents(tmp_path / "state.db", "sess-1")
    assert any("wwplove815" in c for c in contents)
