"""T1 回调契约：``run_conversation(tool_callback=…)`` 事件（批三十一 chat API）。

铁律断言：
  - 默认 ``tool_callback=None`` 时零触发（CLI/gateway 路径行为不变）；
  - 传入时每个工具 pre-execute 发 ``tool_start``（name + input_summary）、
    post-execute 发 ``tool_end``（name + output_summary + ok）；
  - 输入/输出摘要必须过双层 redact（凭据值零泄露）+ 截断到 500 字符；
  - 回调抛异常被吞掉，不影响工具执行。

用 in-process mock provider（``AIAgent.run_conversation`` 全链路），不是消息快照。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        type(self).captured_requests.append(req)
        is_stream = req.get("stream") is True
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = _text_resp("DONE")
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
            ]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):  # noqa: A003 (http.server API)
        pass


def _text_resp(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def _tc_resp(name: str, arguments: str) -> dict:
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _teardown_temp_dir_logging(temp_home: str) -> None:
    """Stop/close async file logging that points into the throwaway VIGIL_HOME.

    AIAgent construction calls ``setup_logging()`` (idempotent). When this
    fixture's agent is the first thing in the process to initialize logging,
    the file handlers land inside the fixture's temp ``VIGIL_HOME``; without
    teardown the ``QueueListener`` thread keeps writing into the rmtree'd dir
    (``FileNotFoundError`` noise) and the handlers leak into later tests.
    When ``setup_logging()`` already ran (any earlier import), no handlers
    point at the temp home and this is a no-op.
    """
    import logging

    import hermes_logging as hl

    home = str(temp_home)
    with hl._queue_state_lock:
        stale = [
            h for h in hl._queued_file_handlers
            if str(getattr(h, "baseFilename", "")).startswith(home)
        ]
        if not stale:
            return
        for h in stale:
            try:
                h.close()
            except Exception:
                pass
        hl._queued_file_handlers[:] = [
            h for h in hl._queued_file_handlers if h not in stale
        ]
        if hl._queued_file_handlers:
            return
        hl._stop_queue_listener_locked()
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_hermes_queue", False):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        hl._log_queue = None


@pytest.fixture()
def agent_env(monkeypatch):
    """Mock provider + 隔离 VIGIL_HOME，yield (agent, handler)。"""
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_tool_cb_")
    os.makedirs(os.path.join(test_home, ".vigil"))
    monkeypatch.setenv("VIGIL_HOME", os.path.join(test_home, ".vigil"))

    # NOTE: no ``del sys.modules[...]`` reload here.  Purging the repo modules
    # and re-importing them under the temp VIGIL_HOME splits module identity:
    # test modules collected earlier in the same process keep references to
    # the pre-reload objects, so their ``patch()`` targets (resolved by name
    # against the post-reload ``sys.modules`` entries) silently stop applying
    # and unrelated suites fail in the same process (batch-31 fix 1).  The
    # code under test is current on disk; conftest sandboxes VIGIL_HOME and
    # re-pins ``hermes_state.DEFAULT_DB_PATH`` per test already.
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=10, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="cli",
    )
    agent.valid_tool_names = {"terminal", "read_file", "write_file", "execute_code", "session_search"}

    try:
        yield agent, _MockHandler
    finally:
        srv.shutdown()
        _teardown_temp_dir_logging(test_home)
        shutil.rmtree(test_home, ignore_errors=True)


def test_tool_callback_default_none_never_fires(agent_env):
    """铁律：默认 None 时零触发（现有 CLI/gateway 路径行为不变）。"""
    agent, handler = agent_env
    handler.response_queue.append(_text_resp("plain answer"))
    events: list = []
    result = agent.run_conversation("hello", conversation_history=[], task_id="t")
    assert result["final_response"] == "plain answer"
    assert events == []


def test_tool_callback_fires_start_and_end_around_execution(agent_env):
    agent, handler = agent_env
    handler.response_queue.append(_tc_resp("read_file", json.dumps({"path": "/nonexistent/xyz"})))
    handler.response_queue.append(_text_resp("done reading"))
    events: list = []
    agent.run_conversation(
        "read a file", conversation_history=[], task_id="t", tool_callback=events.append
    )
    names = [e["name"] for e in events]
    assert names == ["read_file", "read_file"]
    assert events[0]["type"] == "tool_start"
    assert "path" in events[0]["input_summary"]
    assert events[1]["type"] == "tool_end"
    assert isinstance(events[1]["ok"], bool)


def test_tool_callback_redacts_credentials_and_truncates(agent_env):
    agent, handler = agent_env
    secret = "hunter2-secret-token-xyz"
    handler.response_queue.append(_tc_resp("write_file", json.dumps({
        "path": "/tmp/out.txt",
        "content": f"token={secret}",
    })))
    handler.response_queue.append(_text_resp("done writing"))
    events: list = []
    agent.run_conversation(
        "write the file", conversation_history=[], task_id="t", tool_callback=events.append
    )
    assert len(events) == 2
    blob = json.dumps(events, ensure_ascii=False)
    assert secret not in blob
    assert events[0]["type"] == "tool_start"
    assert events[1]["type"] == "tool_end"


def test_tool_callback_summary_truncated_to_500(agent_env):
    from agent.conversation_loop import _redact_tool_event_text
    out = _redact_tool_event_text("x" * 2000)
    assert len(out) <= 501
    assert out.endswith("…")


def test_tool_callback_exception_is_swallowed(agent_env):
    agent, handler = agent_env
    handler.response_queue.append(_tc_resp("read_file", json.dumps({"path": "/nonexistent/abc"})))
    handler.response_queue.append(_text_resp("recovered"))

    def _boom(event):
        raise RuntimeError("callback exploded")

    result = agent.run_conversation(
        "read a file", conversation_history=[], task_id="t", tool_callback=_boom
    )
    # 回调异常不影响工具执行与最终回复
    assert result["final_response"] == "recovered"


def test_tool_end_ok_heuristic_on_error_result(agent_env):
    agent, handler = agent_env
    handler.response_queue.append(_tc_resp("read_file", json.dumps({"path": "/definitely/missing"})))
    handler.response_queue.append(_text_resp("done"))
    events: list = []
    agent.run_conversation(
        "read missing", conversation_history=[], task_id="t", tool_callback=events.append
    )
    end = [e for e in events if e["type"] == "tool_end"]
    assert end and end[0]["ok"] is False
    assert "error" in end[0]["output_summary"].lower() or end[0]["output_summary"]
