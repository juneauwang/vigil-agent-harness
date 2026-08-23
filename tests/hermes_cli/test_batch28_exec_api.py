"""批二十八：执行/审批/审计/会话/事件 API（契约 ~/notes/vigil-exec-api-draft.md）。

分层：只打 web_server 新增端点（approvals / exec+SSE / audit / sessions /
incidents），不碰 conversation_loop / 缓存 / 压缩。硬约束断言：新增端点不进
PUBLIC_API_PATHS；凭据值在所有端点响应 grep 0 命中；prod B' 门 yolo 绕不过；
审批超时 fail-closed 409。
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

import hermes_cli.config as hc
from hermes_cli import web_server


@pytest.fixture
def client():
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


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    """隔离 VIGIL_HOME + 进程内注册表/执行记录，避免跨测试串扰。"""
    from tools.approval import clear_web_approvals
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    web_server._EXEC_RECORDS.clear()
    clear_web_approvals()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    web_server._EXEC_RECORDS.clear()
    clear_web_approvals()
    from tools import ops_permissions as _op
    _op._WARNED_ENVS.clear()


def _cfg(home, env, *, extra="") -> None:
    (home / "config.yaml").write_text(
        "ops:\n"
        "  permissions:\n"
        "    enabled: true\n"
        f"    env: {env}\n"
        "    role: operator\n"
        f"{extra}\n"
        # DEFAULT_CONFIG 的 approvals.mode 是 smart（会触发 aux LLM 评估，测试
        # 环境无可用 provider → 慢且不确定）；批二十八测试固定 manual 走人工门。
        "approvals:\n"
        "  mode: manual\n"
        "  timeout: 30\n",
        encoding="utf-8",
    )


def _write_matrix(home, matrix) -> None:
    """写测试矩阵（YAPL P5：权限判定 = 动作枚举 × 矩阵）。"""
    (home / "matrix.yaml").write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-08-23T00:00:00+08:00",
        "source": "test",
        "base_template": "template2",
        "matrix": matrix,
        "sources": {env: {act: "test" for act in cells}
                    for env, cells in matrix.items()},
    }), encoding="utf-8")


def _sse_events(client, url):
    """消费 SSE 流，返回 [(event, data_dict), ...]。"""
    events = []
    with client.stream("GET", url) as resp:
        assert resp.status_code == 200, resp.text
        ev = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: ") and ev is not None:
                events.append((ev, json.loads(line[len("data: "):])))
                ev = None
    return events


# ------------------------- 一、Approvals API --------------------------------


def test_approvals_endpoints_not_public():
    from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS
    assert "/api/approvals" not in PUBLIC_API_PATHS
    assert "/api/exec" not in PUBLIC_API_PATHS
    assert "/api/audit/sessions" not in PUBLIC_API_PATHS
    assert "/api/incidents" not in PUBLIC_API_PATHS


def test_approvals_timeout_409_fail_closed(client, env_home):
    from tools.approval import register_web_approval
    aid = register_web_approval(
        command="kubectl delete pod x", description="prod 变更确认门（B'）",
        env="prod", timeout_seconds=1,
    )
    time.sleep(1.1)
    r = client.post(f"/api/approvals/{aid}/approve", json={"scope": "once"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "timeout"
    r2 = client.post(f"/api/approvals/{aid}/deny", json={})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "timeout"
    # 命令保持 pending 语义：条目标记 timeout，不自动批准/拒绝。
    listing = client.get("/api/approvals", params={"status": "timeout"}).json()
    assert any(a["id"] == aid for a in listing["approvals"])


def test_approvals_scope_validation_and_deny(client, env_home):
    from tools.approval import register_web_approval
    aid = register_web_approval(
        command="kubectl apply -f deploy.yaml", description="prod 变更确认门（B'）",
        env="prod", allow_session=False, allow_permanent=False,
    )
    r = client.post(f"/api/approvals/{aid}/approve", json={"scope": "session"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    r = client.post(f"/api/approvals/{aid}/approve", json={"scope": "once"})
    assert r.status_code == 200 and r.json() == {"status": "approved", "scope": "once"}
    # 批四十一 §2 幂等化：已裁决重复批准 → 200 + 当前状态（不报错）。
    r = client.post(f"/api/approvals/{aid}/approve", json={"scope": "once"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert r.json()["scope"] == "once"

    aid2 = register_web_approval(command="rm -rf /tmp/x", description="d", env="test")
    r = client.post(f"/api/approvals/{aid2}/deny", json={"reason": "人工拒绝"})
    assert r.status_code == 200 and r.json() == {"status": "denied"}


def test_approvals_list_filtering_pagination(client, env_home):
    from tools.approval import approve_web_approval, register_web_approval
    ids = [
        register_web_approval(command=f"cmd {i}", description="d", env="test",
                              timeout_seconds=3600)
        for i in range(4)
    ]
    prod_id = register_web_approval(command="prod cmd", description="d", env="prod",
                                    timeout_seconds=3600)
    approve_web_approval(ids[0], scope="once")

    listing = client.get("/api/approvals", params={"limit": 3, "offset": 0}).json()
    assert listing["total"] == 5 and listing["has_more"] is True
    assert len(listing["approvals"]) == 3
    # pending 排前、prod 优先。
    assert listing["approvals"][0]["env"] == "prod"
    assert listing["approvals"][0]["status"] == "pending"

    page2 = client.get("/api/approvals", params={"limit": 3, "offset": 3}).json()
    assert len(page2["approvals"]) == 2 and page2["has_more"] is False

    prod_only = client.get("/api/approvals", params={"env": "prod"}).json()
    assert prod_only["total"] == 1 and prod_only["approvals"][0]["id"] == prod_id

    resolved = client.get("/api/approvals", params={"status": "approved"}).json()
    assert resolved["total"] == 1 and resolved["approvals"][0]["id"] == ids[0]

    assert client.get("/api/approvals", params={"limit": -1}).status_code == 200


# ------------------------- 三、Terminal / 执行 API ---------------------------


def test_exec_benign_executed_and_record(client, env_home):
    _cfg(env_home, "test")
    # YAPL P5：echo 不在规则表 → unknown → 默认 approve；benign 直跑用矩阵
    # execute 档动作（query × test）。
    _write_matrix(env_home, {"test": {"query": "execute"}})
    r = client.post("/api/exec", json={"command": "docker ps", "env": "test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed"
    assert body["exit_code"] == 0
    assert "CONTAINER" in body["output"]
    exec_id = body["exec_id"]

    rec = client.get(f"/api/exec/{exec_id}").json()
    assert rec["exec_id"] == exec_id
    assert rec["status"] == "executed" and rec["exit_code"] == 0
    assert rec["output"] and "CONTAINER" in rec["output"]
    assert rec["command"] == "docker ps"
    assert set(rec) == {
        "exec_id", "command", "env", "host", "session_id", "status",
        "exit_code", "output", "executed_at", "duration_ms", "approval_id",
        "error", "created_at", "timeout_seconds",
    }

    assert client.get("/api/exec/does_not_exist").status_code == 404

    # executed 记录 SSE 重放：start → output → exit。
    events = _sse_events(client, f"/api/exec/{exec_id}/stream")
    names = [e for e, _ in events]
    assert names[0] == "exec:start"
    assert "exec:output" in names and "exec:exit" in names
    exit_ev = [d for e, d in events if e == "exec:exit"][0]
    assert exit_ev["exit_code"] == 0


def test_exec_validation_errors(client, env_home):
    _cfg(env_home, "test")
    _write_matrix(env_home, {"test": {"query": "execute"}})
    assert client.post("/api/exec", json={}).status_code == 400
    r = client.post("/api/exec", json={"command": "ls", "host": "node1"})
    assert r.status_code == 400
    assert "远端" in r.json()["error"]["message"]
    r = client.post("/api/exec", json={"command": "ls", "timeout_seconds": "abc"})
    assert r.status_code == 400
    # host 显式本机允许。
    r = client.post("/api/exec", json={"command": "docker ps", "host": "localhost",
                                       "env": "test"})
    assert r.status_code == 200 and r.json()["status"] == "executed"


def test_exec_prod_unknown_defaults_approve_then_deny(client, env_home):
    """YAPL P5：iptables -F 不在规则表 → unknown → 默认 approve（矩阵无 deny）→
    needs_approval 弹窗；用户 deny → 终态 denied。"""
    _cfg(env_home, "prod")
    r = client.post("/api/exec", json={"command": "iptables -F", "env": "prod"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_approval" and body["pending"] is True
    exec_id, approval_id = body["exec_id"], body["approval_id"]

    entry = next(
        a for a in client.get("/api/approvals").json()["approvals"]
        if a["id"] == approval_id
    )
    assert entry["action"] == "unknown"
    assert entry["grade"] is None  # L1-L4 grade 字段退役（OPS-DELTA #75）
    assert "未能识别命令意图" in entry["description"]

    r = client.post(f"/api/approvals/{approval_id}/deny", json={"reason": "不批"})
    assert r.status_code == 200

    rec = client.get(f"/api/exec/{exec_id}").json()
    assert rec["status"] == "denied" and rec["error"]

    events = _sse_events(client, f"/api/exec/{exec_id}/stream")
    assert events[0][0] == "exec:error"


def test_exec_prod_unknown_approval_chain(client, env_home):
    """echo（识别不出）→ unknown → 默认 approve → 审批弹窗；批准后执行。"""
    _cfg(env_home, "prod")
    r = client.post("/api/exec", json={
        "command": "echo approval-gate-test", "env": "prod",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_approval"
    assert body["pending"] is True
    exec_id, approval_id = body["exec_id"], body["approval_id"]

    listing = client.get("/api/approvals").json()
    entry = next(a for a in listing["approvals"] if a["id"] == approval_id)
    assert entry["env"] == "prod"
    assert entry["action"] == "unknown"
    assert entry["grade"] is None  # grade 字段退役（OPS-DELTA #75）
    assert entry["status"] == "pending"
    # unknown → 普通 approve（非 required），会话/永久作用域可用。
    assert entry["allow_session"] is True and entry["allow_permanent"] is True

    rec = client.get(f"/api/exec/{exec_id}").json()
    assert rec["status"] == "needs_approval" and rec["approval_id"] == approval_id

    # 批准 once → guard 原生完成 allowlist/trajectory → 记录转 approved。
    r = client.post(f"/api/approvals/{approval_id}/approve", json={"scope": "once"})
    assert r.status_code == 200

    events = _sse_events(client, f"/api/exec/{exec_id}/stream")
    names = [e for e, _ in events]
    assert names[0] == "exec:start"
    assert "exec:output" in names and "exec:exit" in names
    assert any("approval-gate-test" in d.get("chunk", "") for e, d in events if e == "exec:output")

    rec2 = client.get(f"/api/exec/{exec_id}").json()
    assert rec2["status"] == "executed" and rec2["exit_code"] == 0


def test_exec_prod_required_yolo_cannot_bypass(client, env_home):
    """{approve: required}（prod restart）→ 强制人工，yolo 也绕不过（B' 已退役，
    required 由矩阵驱动；unknown 命令改为普通 approve）。"""
    from tools.approval import disable_session_yolo, enable_session_yolo
    _cfg(env_home, "prod")
    _write_matrix(env_home, {
        "prod": {"query": "execute", "restart": {"approve": "required"}},
    })
    enable_session_yolo("default")
    try:
        r = client.post("/api/exec", json={
            "command": "systemctl restart myapp", "env": "prod",
        })
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "needs_approval"
        entry = next(
            a for a in client.get("/api/approvals").json()["approvals"]
            if a["id"] == r.json()["approval_id"]
        )
        assert entry["status"] == "pending"
        assert entry["action"] == "restart"
        # required 不提供会话/永久作用域——每次都人工确认。
        assert entry["allow_session"] is False and entry["allow_permanent"] is False
    finally:
        disable_session_yolo("default")


def test_exec_deny_chain(client, env_home):
    _cfg(env_home, "prod")
    r = client.post("/api/exec", json={
        "command": "echo deny-gate-test", "env": "prod",
    })
    assert r.json()["status"] == "needs_approval"
    exec_id, approval_id = r.json()["exec_id"], r.json()["approval_id"]

    r = client.post(f"/api/approvals/{approval_id}/deny", json={"reason": "不批"})
    assert r.status_code == 200

    events = _sse_events(client, f"/api/exec/{exec_id}/stream")
    assert events[0][0] == "exec:error"
    rec = client.get(f"/api/exec/{exec_id}").json()
    assert rec["status"] == "denied"


# ------------------------- 二、Audit / 五、Sessions API ----------------------


def _write_trajectory(home, session_id, events):
    import re
    from agent.trajectory import get_trajectory_dir
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:120]
    path = get_trajectory_dir() / f"{safe}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return path


def test_audit_sessions_events_detail_prune(client, env_home):
    _write_trajectory(env_home, "sess_a", [
        {"ts": "2026-08-01T00:00:00Z", "session_id": "sess_a", "seq": 1,
         "type": "terminal", "tool": "terminal", "action": "ls -la", "result": "exit 0"},
        {"ts": "2026-08-02T00:00:00Z", "session_id": "sess_a", "seq": 2,
         "type": "approval", "action": "kubectl delete pod x", "approval": "approved",
         "result": "scope once"},
        {"ts": "2026-08-03T00:00:00Z", "session_id": "sess_a", "seq": 3,
         "type": "terminal", "action": "x" * 600, "result": "long"},
    ])
    _write_trajectory(env_home, "sess_b", [
        {"ts": "2026-08-10T00:00:00Z", "session_id": "sess_b", "seq": 1,
         "type": "terminal", "action": "df -h", "result": "ok"},
    ])

    sessions = client.get("/api/audit/sessions").json()
    assert sessions["total"] == 2
    by_id = {s["session_id"]: s for s in sessions["sessions"]}
    assert by_id["sess_a"]["event_count"] == 3
    assert by_id["sess_b"]["event_count"] == 1

    events = client.get("/api/audit/events", params={"type": "terminal"}).json()
    assert events["total"] == 3
    assert all(e["type"] == "terminal" for e in events["events"])

    events_a = client.get("/api/audit/events", params={"session_id": "sess_a",
                                                       "type": "terminal"}).json()
    assert events_a["total"] == 2
    # preview 截断 500 字符。
    long_ev = next(e for e in events_a["events"] if e["seq"] == 3)
    assert long_ev["action"].endswith("…") and len(long_ev["action"]) == 501
    assert events_a["has_more"] is False

    detail = client.get("/api/audit/events/sess_a/3").json()
    assert len(detail["action"]) == 600
    assert client.get("/api/audit/events/sess_a/99").status_code == 404

    # 分页。
    page = client.get("/api/audit/events", params={"limit": 1, "offset": 0}).json()
    assert page["total"] == 4 and page["has_more"] is True

    # prune：older_than 2026-08-05 → 删 sess_a（最后活动 08-03 < 08-05）。
    r = client.delete("/api/audit/events", params={"older_than": "2026-08-05T00:00:00Z"})
    assert r.json() == {"status": "pruned", "deleted": 1}
    assert client.get("/api/audit/sessions").json()["total"] == 1

    # 非法 older_than → 400。
    r = client.delete("/api/audit/events", params={"older_than": "not-a-date"})
    assert r.status_code == 400


def test_session_events_endpoint(client, env_home):
    _write_trajectory(env_home, "sess_x", [
        {"ts": "2026-08-01T00:00:00Z", "session_id": "sess_x", "seq": 1,
         "type": "terminal", "action": "echo one", "result": "ok"},
        {"ts": "2026-08-02T00:00:00Z", "session_id": "sess_x", "seq": 2,
         "type": "terminal", "action": "echo two", "result": "ok"},
    ])
    r = client.get("/api/sessions/sess_x/events").json()
    assert r["total"] == 2
    assert [e["seq"] for e in r["events"]] == [1, 2]


def test_sessions_contract_shape(client, env_home):
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        db.create_session(session_id="sess_contract_1", source="cli", model="gpt-x")
    finally:
        db.close()
    data = client.get("/api/sessions", params={"limit": 20}).json()
    assert data["total"] >= 1
    row = next(s for s in data["sessions"] if s.get("session_id") == "sess_contract_1")
    # 契约字段。
    for key in ("session_id", "title", "started_at", "last_activity_at", "model", "running"):
        assert key in row, key
    assert row["model"] == "gpt-x"
    # 既有桌面消费字段保留（id/last_active 别名语义）。
    assert row.get("id") == "sess_contract_1"
    assert "last_active" in row


def test_incidents_placeholder(client, env_home):
    # 批五十：/api/incidents 从结构占位（schema 1）升级为 watch inbox 消费层
    # （schema 2）。空 inbox → 空列表 200，不 500。
    r = client.get("/api/incidents").json()
    assert r == {
        "incidents": [], "total": 0, "schema_version": 2,
        "limit": 50, "offset": 0, "has_more": False,
    }


# ------------------------- 安全：凭据零泄露 --------------------------------


def test_credential_zero_leak_all_endpoints(client, env_home):
    _cfg(env_home, "test")
    secret_cmd = (
        'echo "PASSWORD=hunter2"; echo "API_TOKEN=sk-abc123def456"; '
        "echo 'https://admin:s3cret-pw@example.com/x'"
    )
    # YAPL P5：echo 链全 unknown → 默认 approve → 审批弹窗；批准后执行。
    r = client.post("/api/exec", json={"command": secret_cmd, "env": "test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "needs_approval"
    approval_id = body["approval_id"]
    r = client.post(f"/api/approvals/{approval_id}/approve", json={"scope": "once"})
    assert r.status_code == 200
    exec_id = body["exec_id"]

    events = _sse_events(client, f"/api/exec/{exec_id}/stream")
    assert events[0][0] == "exec:start"
    assert any(e == "exec:exit" for e, _ in events)

    rec = client.get(f"/api/exec/{exec_id}").json()
    assert rec["status"] == "executed"

    all_text = " ".join([
        json.dumps(body, ensure_ascii=False),
        json.dumps(rec, ensure_ascii=False),
        json.dumps(events, ensure_ascii=False),
        json.dumps(client.get("/api/audit/events").json(), ensure_ascii=False),
        json.dumps(client.get("/api/audit/sessions").json(), ensure_ascii=False),
    ])
    for secret in ("hunter2", "sk-abc123def456", "s3cret-pw"):
        assert secret not in all_text, f"凭据泄露: {secret}"


def test_credential_zero_leak_approvals_command(client, env_home):
    """审批条目 command 含疑似凭据先打码再展示。"""
    _cfg(env_home, "prod")
    r = client.post("/api/exec", json={
        "command": "echo deploy.yaml --token=sk-abc123def456",
        "env": "prod",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "needs_approval"
    approval_id = r.json()["approval_id"]

    listing = client.get("/api/approvals").json()
    entry = next(a for a in listing["approvals"] if a["id"] == approval_id)
    assert "sk-abc123def456" not in json.dumps(entry, ensure_ascii=False)
    assert "--token=***" in entry["command"] or "***" in entry["command"]

    rec = client.get(f"/api/exec/{r.json()['exec_id']}").json()
    assert "sk-abc123def456" not in json.dumps(rec, ensure_ascii=False)
