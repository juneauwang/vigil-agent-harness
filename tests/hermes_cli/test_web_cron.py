"""Dashboard cron API 契约（D2 /schedules 页依赖）：PUT 非法值 → 400 带人话。

覆盖：
  - 非法 schedule（`bad`）→ 400 + `{"detail": "<原始 message>"}`，job 不被改；
  - 合法 updates（name / script_args）→ 200；
  - script_args 类型不对 → 400（不是 500，也不静默写坏 job）。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    """隔离 home + profile 解析（否则会扫到 ~/.vigil 的真 profile）。"""
    (tmp_path / "cron").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: tmp_path / "profiles")
    return tmp_path


@pytest.fixture
def client(cron_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def _create_job(home, **overrides):
    from cron.jobs import create_job, use_cron_store

    kwargs = {
        "prompt": "run digest",
        "schedule": "every 10m",
        "name": "digest-job",
        "script": "builtin:digest",
        "script_args": ["--window", "7d", "--format", "facts"],
        "no_agent": True,
    }
    kwargs.update(overrides)
    with use_cron_store(home):
        return create_job(**kwargs)


def test_put_invalid_schedule_returns_400(client, cron_home):
    job = _create_job(cron_home)

    resp = client.put(
        f"/api/cron/jobs/{job['id']}",
        json={"updates": {"schedule": "bad"}},
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "bad" in detail or "Invalid" in detail or "duration" in detail.lower()

    # job 未被改坏
    from cron.jobs import use_cron_store, get_job

    with use_cron_store(cron_home):
        still = get_job(job["id"])
    assert still["schedule"]["kind"] == "interval"


def test_put_valid_updates_returns_200(client, cron_home):
    job = _create_job(cron_home)

    resp = client.put(
        f"/api/cron/jobs/{job['id']}",
        json={"updates": {"schedule": "every 15m", "name": "renamed-digest"}},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "renamed-digest"
    assert body["schedule"]["kind"] == "interval"
    assert body["schedule"]["minutes"] == 15


def test_put_script_args_roundtrip_and_type_check(client, cron_home):
    job = _create_job(cron_home)

    resp = client.put(
        f"/api/cron/jobs/{job['id']}",
        json={"updates": {"script_args": ["--window", "1d", "--format", "facts"]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["script_args"] == ["--window", "1d", "--format", "facts"]

    bad = client.put(
        f"/api/cron/jobs/{job['id']}",
        json={"updates": {"script_args": "oops"}},
    )
    assert bad.status_code == 400, bad.text
    assert "script_args" in bad.json()["detail"]
