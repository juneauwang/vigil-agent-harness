"""task33 — Incidents 人工处置（ack / clear）状态外置到 sqlite。

契约：``/api/incidents`` 叠加 ``mark`` 字段；``POST /api/incidents/mark``
（ack|clear|unmark，_require_token）

- clear 命中 → 该 (alert_key, episode) 的任何快照都不显示（列表 + total 都剔除）
- ack 命中 → 保留 + ``mark="ack"``
- episode = startsAt：startsAt 变（复发）→ 标记失效、告警重新出现
- 幂等：同一 (key, episode, action) 重复写 → 表里 1 行；unmark → 恢复显示
- **采集层与 inbox 快照文件零改动**：ack/clear 绝不写回快照、绝不碰 ``processed``

最后一组（j）把主 session 复现脚本的 A/B/C 三缺陷断言化——跑真代码
（真 ``watch_collect.collect()`` + 真 ``get_incidents``），临时 ``VIGIL_HOME``
隔离，仅 stub 掉网络 ``_fetch_alerts``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient

from hermes_cli import web_server


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


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    import hermes_cli.config as hc

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    # 全局 conftest 会把 VIGIL_HOME 指到 <tmp_path>/hermes_test 并 pin
    # hermes_state.DEFAULT_DB_PATH；本 fixture 覆盖 VIGIL_HOME 后必须把
    # DEFAULT_DB_PATH 一起对齐，否则 inbox（get_hermes_home）与 state.db
    # （_default_db_path）落在两个不同根。
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    hc._LOAD_CONFIG_CACHE.clear()
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: anthropic/claude-opus-4.8\n  provider: openrouter\n"
        "ops:\n  watch:\n    enabled: true\n"
        "  prometheus:\n    endpoint: http://stub:9090\n"
        "    alertmanager: http://stub:9093\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


_ALERT_CPU = {
    "alertname": "HighCPU",
    "severity": "critical",
    "instance": "host-a:9090",
    "startsAt": "2026-08-21T00:00:00Z",
    "state": "active",
}
_ALERT_DISK = {
    "alertname": "DiskPressure",
    "severity": "warning",
    "instance": "host-b:9100",
    "startsAt": "2026-08-21T01:00:00Z",
    "state": "firing",
}


def _write_inbox(tmp_path, timestamp, alerts, processed=False):
    inbox = tmp_path / "watch" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    collected_at = f"2026-08-21T{timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}Z"
    (inbox / f"{timestamp}.json").write_text(
        json.dumps(
            {"collected_at": collected_at, "alerts": alerts, "processed": processed},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _mark_body(action, alert):
    return {
        "action": action,
        "alertname": alert["alertname"],
        "instance": alert["instance"],
        "startsAt": alert["startsAt"],
    }


def _marks_db_path(tmp_path):
    import hermes_state

    return Path(hermes_state._default_db_path())


def _marks_rows(tmp_path):
    from tools.watch_marks import _open_db

    if not _marks_db_path(tmp_path).exists():
        return []
    db = _open_db(read_only=True)
    try:
        return [
            dict(r)
            for r in db._conn.execute(
                "SELECT alert_key, episode, action FROM incident_marks"
            ).fetchall()
        ]
    finally:
        db.close()


# ── a. ack → GET 带 mark="ack" ────────────────────────────────────────────


def test_ack_marks_incident(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    r = client.post("/api/incidents/mark", json=_mark_body("ack", _ALERT_CPU))
    assert r.status_code == 200

    data = r.json()  # POST 直接返回刷新后的同构列表
    assert data["total"] == 1
    assert data["incidents"][0]["mark"] == "ack"

    got = client.get("/api/incidents").json()
    assert got["incidents"][0]["mark"] == "ack"
    assert got["incidents"][0]["processed"] is False


# ── b. clear → 不返回 + total 减 1 ───────────────────────────────────────


def test_clear_removes_incident(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU, _ALERT_DISK])
    assert client.get("/api/incidents").json()["total"] == 2

    r = client.post("/api/incidents/mark", json=_mark_body("clear", _ALERT_CPU))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert [i["alertname"] for i in data["incidents"]] == ["DiskPressure"]

    got = client.get("/api/incidents").json()
    assert got["total"] == 1
    assert [i["alertname"] for i in got["incidents"]] == ["DiskPressure"]


# ── c. episode 变（startsAt 变）→ 重新出现 ───────────────────────────────


def test_mark_does_not_survive_new_episode(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    client.post("/api/incidents/mark", json=_mark_body("clear", _ALERT_CPU))
    assert client.get("/api/incidents").json()["total"] == 0

    # 同一 alertname|instance，startsAt 变 = 恢复后复发的新 episode。
    recured = {**_ALERT_CPU, "startsAt": "2026-08-22T09:00:00Z"}
    _write_inbox(env_home, "20260822T090000Z000000", [recured])
    got = client.get("/api/incidents").json()
    assert got["total"] == 1
    assert got["incidents"][0]["mark"] is None


# ── d. 幂等：重复 ack → 表里 1 行 ────────────────────────────────────────


def test_ack_is_idempotent(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    body = _mark_body("ack", _ALERT_CPU)
    assert client.post("/api/incidents/mark", json=body).status_code == 200
    assert client.post("/api/incidents/mark", json=body).status_code == 200

    rows = _marks_rows(env_home)
    assert len(rows) == 1
    assert rows[0]["action"] == "ack"
    assert rows[0]["alert_key"] == "HighCPU|host-a:9090"
    assert rows[0]["episode"] == "2026-08-21T00:00:00Z"


# ── e. unmark → 恢复显示 ─────────────────────────────────────────────────


def test_unmark_restores_incident(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    client.post("/api/incidents/mark", json=_mark_body("clear", _ALERT_CPU))
    assert client.get("/api/incidents").json()["total"] == 0

    r = client.post("/api/incidents/mark", json=_mark_body("unmark", _ALERT_CPU))
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert client.get("/api/incidents").json()["incidents"][0]["mark"] is None


def test_unmark_undoes_ack(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    client.post("/api/incidents/mark", json=_mark_body("ack", _ALERT_CPU))
    assert client.get("/api/incidents").json()["incidents"][0]["mark"] == "ack"

    client.post("/api/incidents/mark", json=_mark_body("unmark", _ALERT_CPU))
    got = client.get("/api/incidents").json()
    assert got["incidents"][0]["mark"] is None
    assert _marks_rows(env_home) == []


# ── f. 回归：mark 不碰快照文件、不改 processed ───────────────────────────


def test_mark_never_touches_inbox_snapshot(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    snapshot = env_home / "watch" / "inbox" / "20260821T000000Z000000.json"
    before = json.loads(snapshot.read_text(encoding="utf-8"))

    client.post("/api/incidents/mark", json=_mark_body("ack", _ALERT_CPU))
    client.post("/api/incidents/mark", json=_mark_body("clear", _ALERT_CPU))

    files = sorted(p.name for p in (env_home / "watch" / "inbox").glob("*.json"))
    assert files == ["20260821T000000Z000000.json"]
    after = json.loads(snapshot.read_text(encoding="utf-8"))
    assert after == before  # 字节级不变
    assert after["processed"] is False


# ── g. 新 home 起 SessionDB → incident_marks 表自动建 ────────────────────


def test_incident_marks_table_autocreates(client, env_home):
    # 写一条 mark 触发可写连接 + schema init。
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    client.post("/api/incidents/mark", json=_mark_body("ack", _ALERT_CPU))

    from tools.watch_marks import _open_db

    db = _open_db(read_only=True)
    try:
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(incident_marks)")]
        assert set(cols) == {"alert_key", "episode", "action", "created_at", "actor"}
    finally:
        db.close()


# ── h. 无 inbox / 无 state.db → 200 空列表，不 500 ──────────────────────


def test_missing_inbox_and_state_db_returns_empty(client, env_home):
    assert not (env_home / "state.db").exists()
    r = client.get("/api/incidents")
    assert r.status_code == 200
    assert r.json()["incidents"] == [] and r.json()["total"] == 0


# ── i. 无 token → 401；未知 action / 缺 alertname → 400 ─────────────────


def test_mark_requires_token(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    # 不带 token 头的独立 client（fixture 的 client 自带 token 头）。
    anon = TestClient(web_server.app)
    r = anon.post("/api/incidents/mark", json=_mark_body("ack", _ALERT_CPU))
    assert r.status_code == 401
    # 未授权请求未落任何标记。
    assert _marks_rows(env_home) == []


def test_mark_rejects_bad_payload(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    bad_action = client.post(
        "/api/incidents/mark",
        json={**_mark_body("ack", _ALERT_CPU), "action": "silence"},
    )
    assert bad_action.status_code == 400
    missing_name = client.post(
        "/api/incidents/mark", json={"action": "ack", "instance": "host-a:9090"}
    )
    assert missing_name.status_code == 400
    assert client.get("/api/incidents").json()["total"] == 1


# ── j. 主 session 复现的 A/B/C 场景断言化（真采集 + 真消费）──────────────


@pytest.fixture()
def am_stub(monkeypatch):
    """stub 掉网络 ``_fetch_alerts``，用可变列表模拟 Alertmanager 状态。"""
    import tools.watch_collect as wc

    state = {"now": [dict(_ALERT_CPU)]}
    monkeypatch.setattr(
        wc, "_fetch_alerts", lambda alertmanager, prom_cfg: [dict(a) for a in state["now"]]
    )
    return state


def _inbox_files(env_home):
    inbox = env_home / "watch" / "inbox"
    return sorted(p.name for p in inbox.glob("*.json")) if inbox.exists() else []


def test_scenario_a_ack_survives_recollection(client, env_home, am_stub):
    """缺陷 A：ack 不写快照文件；采集层再写新快照也不会让 ✓ 丢失。

    天真做法（把快照标 ``processed=true``）会让去重失效 → 采集层再写一条
    → 页面又冒出来、✓ 丢失。正确做法：ack 落 state.db，快照字节不变，
    故采集层按未处理快照去重（这里不会新增文件），即使真有新快照进来，
    消费层仍带 ``mark="ack"``。
    """
    import tools.watch_collect as wc

    wc.collect()  # 轮次 1：写入 1 条快照
    assert len(_inbox_files(env_home)) == 1

    client.post("/api/incidents/mark", json=_mark_body("ack", _ALERT_CPU))

    wc.collect()  # 轮次 2：告警仍活跃，但快照未处理 → 去重跳过（未新增文件）
    assert len(_inbox_files(env_home)) == 1
    for path in (env_home / "watch" / "inbox").glob("*.json"):
        assert json.loads(path.read_text(encoding="utf-8"))["processed"] is False

    # 模拟采集层又写一条同 episode 快照（AM 持续活跃 + 老快照被消费的形态）：
    # 消费层仍是 1 条（去重）+ 带 ack（bug 复现里这里会丢 ✓ 并再冒出来）。
    _write_inbox(env_home, "20260821T030000Z000000", [dict(_ALERT_CPU)])
    got = client.get("/api/incidents").json()
    assert got["total"] == 1
    assert got["incidents"][0]["mark"] == "ack"


def test_scenario_bc_clear_survives_recollection(client, env_home, am_stub):
    """缺陷 B/C：clear 后即使 Alertmanager 再报到也不显示；episode 变了才重现。"""
    import tools.watch_collect as wc

    wc.collect()
    client.post("/api/incidents/mark", json=_mark_body("clear", _ALERT_CPU))
    assert client.get("/api/incidents").json()["total"] == 0

    wc.collect()  # 告警仍活跃，但快照未处理 → 去重跳过；clear 依然生效
    assert client.get("/api/incidents").json()["total"] == 0

    # 模拟采集层又写一条同 episode 快照（AM 再报到）→ clear 依旧挡住。
    _write_inbox(env_home, "20260821T030000Z000000", [dict(_ALERT_CPU)])
    assert client.get("/api/incidents").json()["total"] == 0

    # 缺陷 C：告警在 Alertmanager 已恢复（列表空）→ 采集层不写盘，
    # 但 clear 仍把旧快照挡住（不再永久 active）。
    am_stub["now"] = []
    wc.collect()
    wc.collect()
    assert client.get("/api/incidents").json()["total"] == 0

    # 复发（startsAt 变）→ 新 episode，重新出现，标记自动失效。
    # 清掉旧快照：消费层按 collected_at 去重，模拟"最新一次采集"就是新 episode。
    for stale in (env_home / "watch" / "inbox").glob("*.json"):
        stale.unlink()
    _write_inbox(
        env_home, "20260822T090000Z000000",
        [{**_ALERT_CPU, "startsAt": "2026-08-22T09:00:00Z"}],
    )
    got = client.get("/api/incidents").json()
    assert got["total"] == 1
    assert got["incidents"][0]["startsAt"] == "2026-08-22T09:00:00Z"
    assert got["incidents"][0]["mark"] is None
