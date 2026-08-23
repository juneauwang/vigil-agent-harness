"""批六十四：GET /api/chat/usage（当前会话实时 token + 价格三级来源费用）。

真实 temp VIGIL_HOME + SessionDB 建行（照 test_batch31_chat_api.py 分层：
agent 用 stub 不触发真 LLM），断言实时 token、费用计算、无价格 cost null、
404、注册表会话级模型优先、manual 覆盖生效。
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import chat_api, web_server


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
    from tools.approval import clear_web_approvals
    import hermes_cli.config as hc
    import tools.pricing as pricing

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    pricing._pricing_cache = {"path": None, "mtime": None, "size": None, "data": None}
    clear_web_approvals()
    (tmp_path / "config.yaml").write_text(
        "approvals:\n  mode: manual\n  timeout: 30\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    pricing._pricing_cache = {"path": None, "mtime": None, "size": None, "data": None}
    clear_web_approvals()


def _create_session_row(session_id: str, *, model: str = "deepseek-v4-flash", input_tokens: int = 0, output_tokens: int = 0):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(session_id, source="web", model=model)
        db.update_token_counts(
            session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            absolute=True,
        )
    finally:
        db.close()


def _get_usage(client, session_id: str):
    return client.get("/api/chat/usage", params={"session_id": session_id})


def test_usage_realtime_tokens_and_cost(client):
    _create_session_row("chat_test1", model="deepseek-v4-flash", input_tokens=15248, output_tokens=76)
    resp = _get_usage(client, "chat_test1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["session_id"] == "chat_test1"
    assert body["model"] == "deepseek-v4-flash"
    assert body["input_tokens"] == 15248
    assert body["output_tokens"] == 76
    assert body["total_tokens"] == 15324
    assert body["checked_at"]
    # 内置兜底（usage_pricing 官方快照 0.14/0.28）→ 费用 ≈ 0.0022
    assert body["cost"] == pytest.approx(0.002156)
    assert body["cost_currency"] == "usd"
    assert body["price_source"] == "builtin"
    assert body["price"]["input_per_1m"] == 0.14
    assert body["price"]["output_per_1m"] == 0.28


def test_usage_unknown_model_cost_null(client):
    _create_session_row("chat_noprice", model="totally-unknown-model-xyz", input_tokens=500, output_tokens=100)
    resp = _get_usage(client, "chat_noprice")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input_tokens"] == 500
    assert body["output_tokens"] == 100
    assert body["cost"] is None
    assert body["price"] is None
    assert body["price_source"] is None


def test_usage_session_not_found_404(client):
    resp = _get_usage(client, "chat_does_not_exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_usage_live_registry_model_precedence(client):
    # 行内 model 是首个计费模型（COALESCE 语义）；活会话注册表的会话级模型
    # 优先（切换模型后按当前模型计价）。
    _create_session_row("chat_live", model="deepseek-v4-flash", input_tokens=100, output_tokens=50)
    chat_api._CHAT_SESSIONS["chat_live"] = chat_api.ChatSession(
        chat_session_id="chat_live",
        agent=None,
        session_db=None,
        model="m-other-model",
    )
    resp = _get_usage(client, "chat_live")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "m-other-model"
    assert body["cost"] is None  # 未知模型 → 无价格，token 照常
    assert body["input_tokens"] == 100


def test_usage_manual_pricing_wins(tmp_path, client):
    import yaml

    (tmp_path / "pricing.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "updated_at": "2026-08-23T00:00:00Z",
                "prices": {
                    "deepseek-v4-flash": {
                        "input_per_1m": 0.10,
                        "output_per_1m": 0.20,
                        "currency": "usd",
                        "source": "manual",
                    },
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    _create_session_row("chat_manual", model="deepseek-v4-flash", input_tokens=1000, output_tokens=200)
    resp = _get_usage(client, "chat_manual")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price_source"] == "manual"
    # 1000/1e6*0.10 + 200/1e6*0.20 = 0.00014
    assert body["cost"] == pytest.approx(0.00014)
