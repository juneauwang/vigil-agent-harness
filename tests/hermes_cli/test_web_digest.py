"""Le Registre（D3）后端契约：/api/digest/* 配置、凭据、连接测试、台账、生成。

覆盖任务书「必测」7 条：
  1. GET config 未配置 → 空字段 + configured:false，不含令牌字段值；
  2. PUT config 非法 window/target/base_url → 400 且 config 未改；
  3. PUT config 合法 → 200 可读回；只改传入字段；
  4. PUT credential → vault 真能 retrieve，响应不含值；
  5. POST test 假 BookStack：成功回书名；401 时 detail 脱敏（不含 token）；
  6. GET reports 只列 *.md；reports/{name} 对穿越名 400/404；
  7. POST generate → 后台线程按 facts → md 顺序两次 fire，返回 started。

安全红线同样在断言里：任何响应体都不出现令牌值。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def digest_home(tmp_path, monkeypatch):
    home = tmp_path / "vigil-home"
    (home / "reports").mkdir(parents=True)
    (home / "cron").mkdir(parents=True)
    (home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))

    import hermes_cli.config as hc

    hc._LOAD_CONFIG_CACHE.clear()
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: home / "profiles")
    try:
        yield home
    finally:
        hc._LOAD_CONFIG_CACHE.clear()


@pytest.fixture
def client(digest_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.config import load_config_readonly

    load_config_readonly()  # 先建缓存，模拟运行期
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def _fresh_get(client, path: str):
    """每次读配置前丢一次进程内缓存（模拟真实请求的 mtime 命中路径之外的情况）。"""
    import hermes_cli.config as hc

    hc._LOAD_CONFIG_CACHE.clear()
    return client.get(path)


def _config(home: Path) -> dict:
    import yaml

    raw = (home / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {}


# ---------------------------------------------------------------------------
# 1/2/3：配置读写与校验
# ---------------------------------------------------------------------------

def test_get_config_unconfigured_has_no_secret(client, digest_home):
    resp = _fresh_get(client, "/api/digest/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["window"] == ""
    assert data["publish"]["target"] == "none"
    bookstack = data["publish"]["bookstack"]
    assert bookstack["base_url"] == ""
    assert bookstack["book_id"] == 0
    assert bookstack["credential_ref"] == "bookstack-api"
    assert bookstack["configured"] is False

    text = resp.text.lower()
    for needle in ("token_secret", "token_id", "secret=", "password"):
        assert needle not in text, f"响应里出现了令牌相关字段：{needle}"


def test_put_config_rejects_invalid_values(client, digest_home):
    before = _config(digest_home)

    bad_window = client.put("/api/digest/config", json={"window": "bad"})
    assert bad_window.status_code == 400, bad_window.text
    assert bad_window.json()["error"]["code"] == "invalid_window"
    assert bad_window.json()["detail"]

    bad_target = client.put("/api/digest/config", json={"target": "ftp"})
    assert bad_target.status_code == 400, bad_target.text
    assert bad_target.json()["error"]["code"] == "invalid_target"

    bad_url = client.put("/api/digest/config", json={"base_url": "ftp://bs.local"})
    assert bad_url.status_code == 400, bad_url.text
    assert bad_url.json()["error"]["code"] == "invalid_base_url"

    bad_id = client.put("/api/digest/config", json={"book_id": "not-a-number"})
    assert bad_id.status_code == 400, bad_id.text
    assert bad_id.json()["error"]["code"] == "invalid_book_id"

    bad_ref = client.put("/api/digest/config", json={"credential_ref": "Bad Name"})
    assert bad_ref.status_code == 400, bad_ref.text
    assert bad_ref.json()["error"]["code"] == "invalid_credential_ref"

    # 全被拒 → config.yaml 一个字段都没写
    assert _config(digest_home) == before
    data = _fresh_get(client, "/api/digest/config").json()["data"]
    assert data["window"] == "" and data["publish"]["target"] == "none"


def test_put_config_partial_update_roundtrip(client, digest_home):
    resp = client.put("/api/digest/config", json={
        "window": "1d",
        "target": "file",
        "base_url": "http://bookstack.local/",
        "book_id": 7,
        "credential_ref": "bs-prod",
    })
    assert resp.status_code == 200, resp.text

    data = _fresh_get(client, "/api/digest/config").json()["data"]
    assert data["window"] == "1d"
    assert data["publish"]["target"] == "file"
    assert data["publish"]["bookstack"]["base_url"] == "http://bookstack.local"
    assert data["publish"]["bookstack"]["book_id"] == 7
    assert data["publish"]["bookstack"]["credential_ref"] == "bs-prod"

    # 只改传入字段：其余保持
    assert client.put("/api/digest/config", json={"window": "2w"}).status_code == 200
    data = _fresh_get(client, "/api/digest/config").json()["data"]
    assert data["window"] == "2w"
    assert data["publish"]["target"] == "file"
    assert data["publish"]["bookstack"]["book_id"] == 7
    assert data["publish"]["bookstack"]["credential_ref"] == "bs-prod"


# ---------------------------------------------------------------------------
# 4：令牌写入 vault（不回显）
# ---------------------------------------------------------------------------

def test_put_credential_stores_value_and_never_echoes(client, digest_home):
    resp = client.put(
        "/api/digest/credential",
        json={"ref": "bookstack-api", "value": "tokid:topsecret"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "ref": "bookstack-api", "configured": True}
    assert "topsecret" not in resp.text
    assert "tokid" not in resp.text

    from tools.credential_vault import retrieve

    assert retrieve("bookstack-api") == "tokid:topsecret"

    # 名字/值非法 → 400
    assert client.put("/api/digest/credential", json={"ref": "Bad Name", "value": "a:b"}).status_code == 400
    assert client.put("/api/digest/credential", json={"ref": "ok-ref", "value": ""}).status_code == 400
    assert client.put("/api/digest/credential", json={"ref": "ok-ref", "value": "no-colon"}).status_code == 400


# ---------------------------------------------------------------------------
# 5：连接测试（假 BookStack；401 脱敏）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _store_credential(client):
    resp = client.put(
        "/api/digest/credential",
        json={"ref": "bookstack-api", "value": "tokid:topsecret"},
    )
    assert resp.status_code == 200, resp.text


def test_test_connection_success_returns_book_name(client, monkeypatch):
    _store_credential(client)
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("headers") or {}))
        return _FakeResponse(200, {"id": 7, "name": "Ops 手册"})

    monkeypatch.setattr("tools.publish_targets.requests.request", fake_request)

    resp = client.post("/api/digest/test", json={
        "base_url": "http://bookstack.local", "book_id": 7,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["book_name"] == "Ops 手册"
    assert body["status"] == 200
    assert calls and calls[0][0] == "GET"
    assert calls[0][1] == "http://bookstack.local/api/books/7"
    assert calls[0][2]["Authorization"] == "Token tokid:topsecret"
    assert "topsecret" not in resp.text


def test_test_connection_401_detail_is_redacted(client, monkeypatch):
    _store_credential(client)

    def fake_request(method, url, **kwargs):
        # 故意把 token 塞进错误正文，验证接口层也会抹掉
        return _FakeResponse(401, {"error": "bad token tokid:topsecret"},
                             text='{"error":"Unauthorized token tokid:topsecret"}')

    monkeypatch.setattr("tools.publish_targets.requests.request", fake_request)

    resp = client.post("/api/digest/test", json={
        "base_url": "http://bookstack.local", "book_id": 7,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == 401
    assert "topsecret" not in resp.text
    assert "***" in body["detail"]


# ---------------------------------------------------------------------------
# 6：台账与路径穿越
# ---------------------------------------------------------------------------

def test_reports_list_and_path_traversal(client, digest_home):
    reports = digest_home / "reports"
    (reports / "Vigil-作业周报-2026-W44.md").write_text("# 周报\n", encoding="utf-8")
    (reports / "Vigil-作业日报-2026-10-28.md").write_text("# 日报\n", encoding="utf-8")
    (reports / "notes.txt").write_text("ignore me", encoding="utf-8")

    resp = _fresh_get(client, "/api/digest/reports")
    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["data"]]
    assert names == ["Vigil-作业周报-2026-W44.md", "Vigil-作业日报-2026-10-28.md"] or set(names) == {
        "Vigil-作业周报-2026-W44.md", "Vigil-作业日报-2026-10-28.md",
    }
    by_name = {item["name"]: item for item in resp.json()["data"]}
    assert by_name["Vigil-作业周报-2026-W44.md"]["granularity"] == "weekly"
    assert by_name["Vigil-作业日报-2026-10-28.md"]["granularity"] == "daily"

    ok = client.get("/api/digest/reports/Vigil-作业周报-2026-W44.md")
    assert ok.status_code == 200
    assert ok.json()["data"]["content"].startswith("# 周报")

    for name in ("..%2Fetc%2Fpasswd", "%2E%2E%2Fetc%2Fpasswd", "notes.txt", "..%5Cwin.ini"):
        resp = client.get(f"/api/digest/reports/{name}")
        assert resp.status_code in (400, 404), (name, resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# 7：生成流水线（后台串行 facts → md）
# ---------------------------------------------------------------------------

def _create_digest_jobs(home: Path):
    from cron.jobs import create_job, use_cron_store

    with use_cron_store(home):
        facts = create_job(
            prompt="写稿", schedule="0 18 * * 5", name="Weekly work log (digest)",
            script="builtin:digest",
            script_args=["--window", "7d", "--format", "facts"], no_agent=True,
        )
        render = create_job(
            prompt="渲染发布", schedule="0 19 * * 5", name="Weekly work log (render)",
            script="builtin:digest",
            script_args=["--window", "7d", "--format", "md", "--publish", "bookstack"],
            no_agent=True,
        )
    return facts, render


def test_generate_fires_facts_then_render(client, digest_home, monkeypatch):
    facts, render = _create_digest_jobs(digest_home)
    fired = []

    def fake_fire(profile, job_id):
        fired.append(job_id)
        return True

    monkeypatch.setattr("hermes_cli.web_server._fire_cron_job_for_profile", fake_fire)

    resp = client.post("/api/digest/generate", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["started"] is True
    assert body["jobs"] == [facts["id"], render["id"]]

    deadline = time.time() + 5
    while len(fired) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert fired == [facts["id"], render["id"]], f"必须写稿先、渲染后：{fired}"


def test_generate_without_digest_jobs_returns_404(client, digest_home):
    resp = client.post("/api/digest/generate", json={})
    assert resp.status_code == 404, resp.text
    assert "digest" in resp.text.lower()


def test_create_pipeline_without_jobs_makes_two(client, digest_home):
    """没有流水线时一键初始化：两条 job、同一窗口、写稿在前渲染在后。"""
    resp = client.post("/api/digest/pipeline", json={"window": "7d"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["window"] == "7d"
    assert data["facts"]["schedule"] == "0 18 * * 5"
    assert data["render"]["schedule"] == "10 18 * * 5"

    from cron.jobs import get_job, use_cron_store

    with use_cron_store(digest_home):
        facts = get_job(data["facts"]["id"])
        render = get_job(data["render"]["id"])
    assert facts["script"] == "builtin:digest"
    assert facts["script_args"] == ["--window", "7d", "--format", "facts"]
    assert facts["no_agent"] is False
    assert facts["enabled_toolsets"] == ["file", "session_search", "skills"]
    assert "prose.json" in facts["prompt"]
    assert render["script_args"][:4] == ["--window", "7d", "--format", "md"]
    assert "--publish" in render["script_args"]
    assert render["no_agent"] is True


def test_create_pipeline_is_idempotent(client, digest_home):
    """已存在就 409，不重复建、不动已有 job。"""
    first = client.post("/api/digest/pipeline", json={})
    assert first.status_code == 200, first.text
    ids = [first.json()["data"]["facts"]["id"], first.json()["data"]["render"]["id"]]

    second = client.post("/api/digest/pipeline", json={})
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "pipeline_exists"
    for job_id in ids:
        assert job_id in second.text

    from cron.jobs import list_jobs, use_cron_store

    with use_cron_store(digest_home):
        digest_jobs = [j for j in list_jobs(True) if j.get("script") == "builtin:digest"]
    assert len(digest_jobs) == 2


def test_create_pipeline_rejects_bad_input(client, digest_home):
    bad_window = client.post("/api/digest/pipeline", json={"window": "week"})
    assert bad_window.status_code == 400, bad_window.text
    assert bad_window.json()["error"]["code"] == "invalid_window"

    once = client.post("/api/digest/pipeline", json={"facts_schedule": "2027-01-01T09:00:00"})
    assert once.status_code == 400, once.text
    assert once.json()["error"]["code"] == "invalid_schedule"

    # 校验失败不能留下半条流水线
    from cron.jobs import list_jobs, use_cron_store

    with use_cron_store(digest_home):
        assert [j for j in list_jobs(True) if j.get("script") == "builtin:digest"] == []


def test_runs_endpoint_lists_recent_executions(client, digest_home, monkeypatch):
    """`/api/digest/runs` 读 cron 执行账本（空账本 → []，不 500）。"""
    _create_digest_jobs(digest_home)
    from cron import executions

    # 账本路径在模块导入时按当时的 home 解析；测试里显式指到临时 home，
    # 保证不碰开发者真实的 ~/.vigil/cron/executions.db。
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", digest_home / "cron" / "executions.db")

    resp = client.get("/api/digest/runs")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


def test_reports_use_publish_ledger_url(client, digest_home):
    """台账链接只认发布账本：没有账本条目 = 未发布、不给链接。"""
    import json

    reports = digest_home / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "Vigil-作业周报-2026-W44.md").write_text("# x\n", encoding="utf-8")
    (reports / "Vigil-作业日报-2026-10-28.md").write_text("# y\n", encoding="utf-8")
    (reports / "publish-index.json").write_text(json.dumps({
        "Vigil-作业周报-2026-W44": {
            "url": "http://127.0.0.1:8080/books/vigil/page/vigil-2026-w44",
            "page_id": 4, "book_id": 1, "at": "2026-10-28T22:00:00+08:00",
        }
    }), encoding="utf-8")

    body = client.get("/api/digest/reports").json()
    items = {item["name"]: item for item in body["data"]}
    posted = items["Vigil-作业周报-2026-W44.md"]
    assert posted["posted"] is True
    assert posted["url"] == "http://127.0.0.1:8080/books/vigil/page/vigil-2026-w44"
    assert posted["page_id"] == 4

    unposted = items["Vigil-作业日报-2026-10-28.md"]
    assert unposted["posted"] is False and unposted["url"] == ""
