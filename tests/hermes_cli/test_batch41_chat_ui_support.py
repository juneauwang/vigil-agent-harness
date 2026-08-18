"""批四十一 — Chat 页 UI 支撑后端（OPS-DELTA #58）。

§5 工具输出零错位：SSE chat:tool/chat:tool_result 携带服务端唯一 tool_id；
历史折叠按工具调用顺序正序挂接（同名单并行不串位）。
§3 推理：history 视图带 reasoning 字段；SSE 转发 chat:reasoning 增量。
§8 模型：GET /api/models 目录 + 创建会话带 model + 切换会话模型。
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


class _StubAgent:
    _session_db = None
    reasoning_callback = None

    def __init__(self, tool_events=None, emit_reasoning=False):
        self.calls = []
        self.switch_calls = []
        self.model = ""
        self.tool_events = tool_events or []
        self.emit_reasoning = emit_reasoning

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
        if sc:
            sc("最终答案")
        return {"final_response": "最终答案", "api_calls": 1, "completed": True, "partial": False}

    def switch_model(self, new_model, new_provider, **kwargs):
        self.switch_calls.append((new_model, new_provider, kwargs))
        self.model = new_model


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


# ── §5 工具输出零错位 ────────────────────────────────────────────────────


def test_tool_sse_events_carry_tool_id(client, stub_agent_factory):
    _install, _ = stub_agent_factory
    agent = _install(tool_events=[
        {"type": "tool_start", "name": "terminal", "input_summary": "nvidia-smi", "tool_id": "call_1"},
        {"type": "tool_start", "name": "terminal", "input_summary": "mysql -e 'select 1'", "tool_id": "call_2"},
        # 结果按完成顺序到达（call_1 先回）——前端必须按 id 挂接。
        {"type": "tool_end", "name": "terminal", "output_summary": "NVIDIA 4090", "ok": True, "tool_id": "call_1"},
        {"type": "tool_end", "name": "terminal", "output_summary": "count=128", "ok": True, "tool_id": "call_2"},
    ])
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", json_body={"message": "看 gpu"})

    tools = [d for (t, d) in events if t == "chat:tool"]
    results = [d for (t, d) in events if t == "chat:tool_result"]
    assert [t["tool_id"] for t in tools] == ["call_1", "call_2"]
    assert [r["tool_id"] for r in results] == ["call_1", "call_2"]
    pair = {r["tool_id"]: r["output_summary"] for r in results}
    assert pair == {"call_1": "NVIDIA 4090", "call_2": "count=128"}


def test_tool_sse_events_fallback_seq_id_without_provider_id(client, stub_agent_factory):
    """提供商不带 tool_id 时：chat_api 按 turn 内顺序补序号（start/end 对齐）。"""
    _install, _ = stub_agent_factory
    _install(tool_events=[
        {"type": "tool_start", "name": "terminal", "input_summary": "a"},
        {"type": "tool_start", "name": "read_file", "input_summary": "/etc/hosts"},
        {"type": "tool_end", "name": "terminal", "output_summary": "out_a", "ok": True},
        {"type": "tool_end", "name": "read_file", "output_summary": "out_b", "ok": True},
    ])
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", json_body={"message": "x"})
    tools = [d for (t, d) in events if t == "chat:tool"]
    results = [d for (t, d) in events if t == "chat:tool_result"]
    assert tools[0]["tool_id"] == results[0]["tool_id"] == "tool-1"
    assert tools[1]["tool_id"] == results[1]["tool_id"] == "tool-2"


def test_history_view_forward_matching_same_name_parallel_tools():
    """历史折叠：同名单并行工具（terminal x2）正序挂接，输出零错位。"""
    history = [
        {"role": "assistant", "content": "", "_row_id": 2,
         "tool_calls": [
             {"id": "call_1", "function": {"name": "terminal", "arguments": "nvidia-smi"}},
             {"id": "call_2", "function": {"name": "terminal", "arguments": "mysql -e 'select 1'"}},
         ]},
        {"role": "tool", "content": "NVIDIA 4090", "_row_id": 3, "tool_name": "terminal", "tool_call_id": "call_1"},
        {"role": "tool", "content": "count=128", "_row_id": 4, "tool_name": "terminal", "tool_call_id": "call_2"},
    ]
    view = chat_api._history_to_view_messages(history)
    tools = view[0]["tools"]
    assert tools[0]["input_summary"].startswith("nvidia")
    assert tools[0]["output_summary"] == "NVIDIA 4090"
    assert tools[1]["input_summary"].startswith("mysql")
    assert tools[1]["output_summary"] == "count=128"


def test_history_view_reasoning_field():
    history = [
        {"role": "assistant", "content": "ok", "_row_id": 1,
         "reasoning": "内部推理文本", "reasoning_content": "内部推理文本",
         "tool_calls": [], "timestamp": 1.0},
    ]
    view = chat_api._history_to_view_messages(history)
    assert view[0]["reasoning"] == "内部推理文本"


# ── §3 推理增量 ──────────────────────────────────────────────────────────


def test_reasoning_sse_events(client, stub_agent_factory):
    # 批四十二 §BJ：无工具调用时推理缓冲到收尾按消息级转发（聚合单事件，
    # 无 tool_id——旧结构兼容；带 tool 的归属另测于 test_batch42）。
    _install, _ = stub_agent_factory
    _install(emit_reasoning=True)
    sid = _new_session(client)
    events = _sse_events(client, f"/api/chat/sessions/{sid}/messages", json_body={"message": "hi"})
    reasoning = [d for (t, d) in events if t == "chat:reasoning"]
    assert [d["text"] for d in reasoning] == ["先想第一步再想第二步"]
    assert all("tool_id" not in d for d in reasoning)
    assert any(t == "chat:done" for (t, _d) in events)


# ── §8 会话级模型 ────────────────────────────────────────────────────────


def test_models_endpoint_returns_catalog(client, monkeypatch):
    resp = client.get("/api/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openrouter"
    assert body["default_model"] == "anthropic/claude-opus-4.8"
    ids = [m["id"] for m in body["models"]]
    assert "anthropic/claude-opus-4.8" in ids  # 配置默认必在目录且标 default
    default_entry = next(m for m in body["models"] if m["id"] == "anthropic/claude-opus-4.8")
    assert default_entry["default"] is True
    # 零敏感信息：无 api_key/token 字段
    flat = json.dumps(body)
    assert "api_key" not in flat and "token" not in flat


def test_create_session_with_model(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install()
    resp = client.post("/api/chat/sessions", json={"model": "openai/gpt-5.5"})
    assert resp.status_code == 200, resp.text
    sid = resp.json()["chat_session_id"]
    assert resp.json()["model"] == "openai/gpt-5.5"
    assert chat_api._CHAT_SESSIONS[sid].model == "openai/gpt-5.5"


def test_create_session_invalid_model_400(client, stub_agent_factory):
    _install, _ = stub_agent_factory
    _install()
    resp = client.post("/api/chat/sessions", json={"model": "not-in-catalog"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_switch_session_model(client, stub_agent_factory):
    _install, holder = stub_agent_factory
    agent = _install()
    sid = _new_session(client)
    resp = client.post(f"/api/chat/sessions/{sid}/model", json={"model": "openai/gpt-5.5"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "openai/gpt-5.5"
    assert agent.switch_calls and agent.switch_calls[0][0] == "openai/gpt-5.5"
    assert chat_api._CHAT_SESSIONS[sid].model == "openai/gpt-5.5"


def test_switch_session_model_busy_409(client, stub_agent_factory, monkeypatch):
    _install, holder = stub_agent_factory
    _install()
    sid = _new_session(client)
    chat_api._CHAT_SESSIONS[sid].busy = True
    resp = client.post(f"/api/chat/sessions/{sid}/model", json={"model": "openai/gpt-5.5"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "busy"


def test_list_sessions_include_model(client, stub_agent_factory):
    _install, _ = stub_agent_factory
    _install()
    sid = _new_session(client)
    resp = client.get("/api/chat/sessions")
    assert resp.status_code == 200
    row = next(s for s in resp.json()["sessions"] if s["id"] == sid)
    assert "model" in row
