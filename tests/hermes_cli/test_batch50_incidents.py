"""批五十 — /api/incidents 接入 watch inbox（OPS-DELTA #9 消费层 → dashboard）。

§INBOX：GET /api/incidents 读 ~/.vigil/watch/inbox/<timestamp>.json，
按 alertname|instance 去重保留最新采集、collected_at 新的在前；
inbox 不存在/为空 → 空列表 200（不 500）。
"""

from __future__ import annotations

import json

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
    hc._LOAD_CONFIG_CACHE.clear()
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: anthropic/claude-opus-4.8\n  provider: openrouter\n",
        encoding="utf-8",
    )
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()


def _write_inbox(tmp_path, timestamp, alerts, processed=False):
    # VIGIL_HOME 指向数据根本身（≈~/.vigil），inbox 在 <root>/watch/inbox。
    inbox = tmp_path / "watch" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    collected_at = f"2026-08-21T{timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}Z"
    (inbox / f"{timestamp}.json").write_text(
        json.dumps(
            {"collected_at": collected_at,
             "alerts": alerts, "processed": processed},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


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


def test_incidents_reads_watch_inbox(client, env_home):
    # 旧采集只有 HighCPU；新采集 HighCPU 复发 + 新 DiskPressure。
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    _write_inbox(env_home, "20260821T010000Z000000", [_ALERT_CPU, _ALERT_DISK])

    r = client.get("/api/incidents").json()
    assert r["total"] == 2  # alertname|instance 去重，HighCPU 只留最新
    assert r["schema_version"] == 2
    assert r["limit"] == 50 and r["offset"] == 0 and r["has_more"] is False
    names = [i["alertname"] for i in r["incidents"]]
    assert names == ["HighCPU", "DiskPressure"]  # 新的 collected_at 在前
    first = r["incidents"][0]
    assert first["collected_at"] == "2026-08-21T01:00:00Z"
    assert first["processed"] is False
    assert first["source"] == "alertmanager"
    assert first["startsAt"] == "2026-08-21T00:00:00Z"
    assert first["state"] == "active"
    assert first["severity"] == "critical"
    assert first["instance"] == "host-a:9090"


def test_incidents_dedupe_keeps_latest_collection(client, env_home):
    # 同键告警在两份 inbox 里 → 只保留 collected_at 最新的那份（含其状态）。
    _write_inbox(env_home, "20260821T000000Z000000",
                 [{**_ALERT_CPU, "state": "firing"}])
    _write_inbox(env_home, "20260821T010000Z000000",
                 [{**_ALERT_CPU, "state": "resolved"}])

    incidents = client.get("/api/incidents").json()["incidents"]
    assert len(incidents) == 1
    assert incidents[0]["collected_at"] == "2026-08-21T01:00:00Z"
    assert incidents[0]["state"] == "resolved"


def test_incidents_processed_flag_passthrough(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU], processed=True)
    incidents = client.get("/api/incidents").json()["incidents"]
    assert incidents[0]["processed"] is True


def test_incidents_pagination(client, env_home):
    _write_inbox(env_home, "20260821T000000Z000000", [_ALERT_CPU])
    _write_inbox(env_home, "20260821T010000Z000000", [_ALERT_DISK])

    page1 = client.get("/api/incidents", params={"limit": 1, "offset": 0}).json()
    assert [i["alertname"] for i in page1["incidents"]] == ["DiskPressure"]
    assert page1["total"] == 2 and page1["has_more"] is True

    page2 = client.get("/api/incidents", params={"limit": 1, "offset": 1}).json()
    assert [i["alertname"] for i in page2["incidents"]] == ["HighCPU"]
    assert page2["total"] == 2 and page2["has_more"] is False


def test_incidents_empty_and_missing_inbox(client, env_home):
    # 无 inbox 目录 → 空列表 200。
    assert client.get("/api/incidents").status_code == 200
    r = client.get("/api/incidents").json()
    assert r == {
        "incidents": [], "total": 0, "schema_version": 2,
        "limit": 50, "offset": 0, "has_more": False,
    }

    # 空 inbox 目录 → 空列表 200。
    (env_home / "watch" / "inbox").mkdir(parents=True, exist_ok=True)
    r = client.get("/api/incidents").json()
    assert r["incidents"] == [] and r["total"] == 0


def test_incidents_skips_corrupt_inbox_files(client, env_home):
    inbox = env_home / "watch" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "20260821T000000Z000000.json").write_text("{not json", encoding="utf-8")
    _write_inbox(env_home, "20260821T010000Z000000", [_ALERT_DISK])

    incidents = client.get("/api/incidents").json()["incidents"]
    assert [i["alertname"] for i in incidents] == ["DiskPressure"]
