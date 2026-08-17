"""批四十一 §2/§6 — 审批幂等化 + 详情端点（OPS-DELTA #58）。

覆盖：重复批准/拒绝返回当前终态（200，不再 invalid_request）；列表/详情端点
字段完整（command/description 全量）；详情端点 404。
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import web_server
from tools.approval import (
    approve_web_approval,
    clear_web_approvals,
    deny_web_approval,
    get_web_approval,
    register_web_approval,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_web_approvals()
    yield
    clear_web_approvals()


@pytest.fixture()
def client(monkeypatch):
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


def _register(**kw) -> str:
    base = dict(
        command="kubectl -n prod rollout restart deploy/x",
        description="生产变更确认门（长描述不被截断：这是一段很长的审批说明文本，"
                    "用于验证批四十一不再用 preview 单向截断丢信息）",
        env="prod",
        grade="L3",
        session_key="sess_1",
        source="web",
    )
    base.update(kw)
    return register_web_approval(**base)


def test_repeat_approve_returns_current_state_no_error():
    aid = _register()
    first = approve_web_approval(aid, scope="once")
    assert first["status"] == "approved" and "code" not in first
    repeat = approve_web_approval(aid, scope="once")
    assert repeat["status"] == "approved"
    assert repeat["already_resolved"] is True
    # 状态不变、scope 不变。
    view = get_web_approval(aid)
    assert view["status"] == "approved" and view["scope"] == "once"


def test_approve_after_deny_returns_denied_state():
    aid = _register()
    deny_web_approval(aid)
    repeat = approve_web_approval(aid, scope="once")
    assert repeat["status"] == "denied"
    assert repeat["already_resolved"] is True
    assert get_web_approval(aid)["status"] == "denied"


def test_deny_after_approve_returns_approved_state():
    aid = _register()
    approve_web_approval(aid, scope="once")
    repeat = deny_web_approval(aid)
    assert repeat["status"] == "approved"
    assert repeat["already_resolved"] is True
    assert get_web_approval(aid)["status"] == "approved"


def test_full_description_stored_not_truncated():
    aid = _register()
    view = get_web_approval(aid)
    assert "长描述不被截断" in view["description"]
    assert "批四十一不再用 preview 单向截断丢信息" in view["description"]


def test_web_endpoint_repeat_approve_200(client):
    aid = _register()
    r1 = client.post(f"/api/approvals/{aid}/approve", json={"scope": "once"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "approved"
    r2 = client.post(f"/api/approvals/{aid}/approve", json={"scope": "once"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "approved"
    r3 = client.post(f"/api/approvals/{aid}/deny", json={})
    assert r3.status_code == 200
    assert r3.json()["status"] == "approved"


def test_web_endpoint_repeat_deny_200(client):
    aid = _register()
    r1 = client.post(f"/api/approvals/{aid}/deny", json={})
    assert r1.status_code == 200
    assert r1.json()["status"] == "denied"
    r2 = client.post(f"/api/approvals/{aid}/deny", json={})
    assert r2.status_code == 200
    assert r2.json()["status"] == "denied"
    r3 = client.post(f"/api/approvals/{aid}/approve", json={"scope": "once"})
    assert r3.status_code == 200
    assert r3.json()["status"] == "denied"


def test_detail_endpoint_returns_full_record(client):
    aid = _register()
    resp = client.get(f"/api/approvals/{aid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == aid
    assert "kubectl -n prod rollout restart deploy/x" == body["command"]
    assert "长描述不被截断" in body["description"]
    assert body["grade"] == "L3"
    assert body["status"] == "pending"
    assert body["env"] == "prod"


def test_detail_endpoint_404(client):
    resp = client.get("/api/approvals/apv_nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_list_returns_full_command_and_description(client):
    aid = _register()
    resp = client.get("/api/approvals")
    assert resp.status_code == 200
    rows = resp.json()["approvals"]
    row = next(r for r in rows if r["id"] == aid)
    assert "rollout restart" in row["command"]
    assert "批四十一不再用 preview 单向截断丢信息" in row["description"]
