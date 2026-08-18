"""批四十二 — Chat 页状态同步族（OPS-DELTA #59）。

§BH 全局审批弹窗/审批中心裁决 → 对话内审批卡同步（前端 pub/sub 广播，后端零改动）。
§BI 结论渲染顺序（纯前端重排，后端零改动）。
§BJ/§BK reasoning 按工具调用粒度挂载：SSE chat:reasoning 带可选 tool_id（推理
缓冲至 tool_start 归属），历史消息 reasoning 结构化 {steps:[{tool_id,text}]}
（向后兼容：0/多工具调用或空推理输出旧单值字符串）。
§AY 模型选择器去用途 tag（纯前端，后端 tag 字段保留兼容）。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _StubAgent:
    _session_db = None
    reasoning_callback = None

    def __init__(self, tool_events=None, emit_reasoning=False, reasoning_after_tools=False):
        self.calls = []
        self.model = ""
        self.tool_events = tool_events or []
        self.emit_reasoning = emit_reasoning
        self.reasoning_after_tools = reasoning_after_tools

    def run_conversation(self, message, **kwargs):
        self.calls.append((message, kwargs))
        sc = kwargs.get("stream_callback")
        tc = kwargs.get("tool_callback")
        if self.emit_reasoning and self.reasoning_callback:
            self.reasoning_callback("先想第一步")
            self.reasoning_callback("再想第二步")
        for ev in self.tool_events:
            if tc:
                tc(ev)
        if self.reasoning_after_tools and self.reasoning_callback:
            self.reasoning_callback("收尾思考最后一步")
        if sc:
            sc("最终答案")
        return {"final_response": "最终答案", "api_calls": 1, "completed": True, "partial": False}


@pytest.fixture()
def client(monkeypatch):
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    chat_api.clear_chat_sessions()
    try:
        yield test_client
    finally:
        chat_api.clear_chat_sessions()
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    import hermes_cli.config as hc

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: anthropic/claude-opus-4.8\n  provider: openrouter\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture()
def stub_agent_factory(monkeypatch):
    holder: dict = {}

    def _install(**kwargs) -> _StubAgent:
        agent = _StubAgent(**kwargs)
        holder["agent"] = agent
        monkeypatch.setattr(chat_api, "_create_chat_agent", lambda sid, model=None: agent)
        return agent

    return _install, holder


def _new_session(client, model=None) -> str:
    body = {"model": model} if model else None
    resp = client.post("/api/chat/sessions", json=body) if body else client.post("/api/chat/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()["chat_session_id"]


def _sse_events(client, url, json_body=None):
    events = []
    with client.stream("POST", url, json=json_body) as resp:
        assert resp.status_code == 200, resp.text
        ev = None
        data_lines = []
        for line in resp.iter_lines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and ev is not None:
                events.append((ev, json.loads("\n".join(data_lines))))
                ev = None
                data_lines = []
    return events


# ── §BJ SSE：推理归属 tool_id ──────────────────────────────────────────────


def test_reasoning_attributed_to_tool_at_tool_start(client, stub_agent_factory):
    """推理缓冲到 tool_start：chat:tool 先推、随后 chat:reasoning 带该 tool_id。"""
    _install, _ = stub_agent_factory
    _install(
        emit_reasoning=True,
        tool_events=[
            {"type": "tool_start", "name": "terminal", "input_summary": "kubectl get nodes", "tool_id": "call_1"},
            {"type": "tool_end", "name": "terminal", "output_summary": "node1 Ready", "ok": True, "tool_id": "call_1"},
        ],
    )
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", json_body={"message": "hi"})
    types = [t for (t, _d) in events]
    assert types.index("chat:tool") < types.index("chat:reasoning") < types.index("chat:tool_result")
    reasoning = [d for (t, d) in events if t == "chat:reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["tool_id"] == "call_1"
    assert reasoning[0]["text"] == "先想第一步再想第二步"


def test_reasoning_without_tool_flushed_as_message_level(client, stub_agent_factory):
    """无工具调用（最终答复前思考）：推理不带 tool_id，按消息级转发。"""
    _install, _ = stub_agent_factory
    _install(emit_reasoning=True)
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", json_body={"message": "hi"})
    reasoning = [d for (t, d) in events if t == "chat:reasoning"]
    assert [d["text"] for d in reasoning] == ["先想第一步再想第二步"]
    assert all("tool_id" not in d for d in reasoning)
    assert any(t == "chat:done" for (t, _d) in events)


def test_reasoning_after_tools_flushed_unattributed(client, stub_agent_factory):
    """工具执行后的收尾推理不向前认领，保留消息级。"""
    _install, _ = stub_agent_factory
    _install(
        emit_reasoning=True,
        reasoning_after_tools=True,
        tool_events=[
            {"type": "tool_start", "name": "read_file", "input_summary": "/etc/hosts", "tool_id": "call_9"},
            {"type": "tool_end", "name": "read_file", "output_summary": "127.0.0.1 localhost", "ok": True, "tool_id": "call_9"},
        ],
    )
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", json_body={"message": "hi"})
    reasoning = [d for (t, d) in events if t == "chat:reasoning"]
    assert [d.get("tool_id", "") for d in reasoning] == ["call_9", ""]
    assert reasoning[0]["text"] == "先想第一步再想第二步"
    assert reasoning[1]["text"] == "收尾思考最后一步"


# ── §BJ 历史 reasoning 结构 ────────────────────────────────────────────────


def test_history_view_reasoning_single_tool_structured():
    """单工具调用消息：reasoning 结构化 {steps:[{tool_id,text}]}。"""
    history = [
        {"role": "assistant", "content": "ok", "_row_id": 1,
         "reasoning": "内部推理文本", "reasoning_content": "内部推理文本",
         "tool_calls": [{"id": "call_1", "function": {"name": "terminal", "arguments": "ls"}}],
         "timestamp": 1.0},
    ]
    view = chat_api._history_to_view_messages(history)
    assert view[0]["reasoning"] == {"steps": [{"tool_id": "call_1", "text": "内部推理文本"}]}


def test_history_view_reasoning_legacy_string_when_no_tool():
    """无工具调用：保持旧单值字符串（向后兼容）。"""
    history = [
        {"role": "assistant", "content": "ok", "_row_id": 1,
         "reasoning": "内部推理文本",
         "tool_calls": [], "timestamp": 1.0},
    ]
    view = chat_api._history_to_view_messages(history)
    assert view[0]["reasoning"] == "内部推理文本"


def test_history_view_reasoning_legacy_string_when_multiple_tools():
    """多工具调用：无法拆分归属，保持旧单值字符串。"""
    history = [
        {"role": "assistant", "content": "ok", "_row_id": 1,
         "reasoning": "内部推理文本",
         "tool_calls": [
             {"id": "call_1", "function": {"name": "terminal", "arguments": "a"}},
             {"id": "call_2", "function": {"name": "terminal", "arguments": "b"}},
         ],
         "timestamp": 1.0},
    ]
    view = chat_api._history_to_view_messages(history)
    assert view[0]["reasoning"] == "内部推理文本"


def test_history_view_reasoning_empty_no_steps_when_no_id():
    """工具调用缺 id：无法归属，保持旧单值字符串。"""
    history = [
        {"role": "assistant", "content": "ok", "_row_id": 1,
         "reasoning": "内部推理文本",
         "tool_calls": [{"id": "", "function": {"name": "terminal", "arguments": "a"}}],
         "timestamp": 1.0},
    ]
    view = chat_api._history_to_view_messages(history)
    assert view[0]["reasoning"] == "内部推理文本"
