"""publish_bookstack / publish_targets 测试（D2）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import bookstack_tool, publish_targets
from tools.publish_targets import (
    ConfluenceTarget,
    FileTarget,
    PublishTargetError,
    build_target,
)

NAME = "Vigil-周报-2026-W44"
MARKDOWN = "# Vigil 周报 2026-W44\n\n正文\n"


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text else json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeRequests:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("BOOKSTACK_URL", "BOOKSTACK_TOKEN_ID", "BOOKSTACK_TOKEN_SECRET", "BOOKSTACK_BOOK_ID"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def configured_env(monkeypatch, clean_env):
    monkeypatch.setenv("BOOKSTACK_URL", "https://bks.example.com/")
    monkeypatch.setenv("BOOKSTACK_TOKEN_ID", "1")
    monkeypatch.setenv("BOOKSTACK_TOKEN_SECRET", "supersecret")
    monkeypatch.setenv("BOOKSTACK_BOOK_ID", "3")
    return monkeypatch


def _patch_requests(monkeypatch, responses):
    fake = FakeRequests(responses)
    monkeypatch.setattr(publish_targets.requests, "request", fake)
    return fake


def _call(args):
    return json.loads(bookstack_tool._handle_publish_bookstack(args))


def test_create_page_with_book_id(configured_env, monkeypatch):
    fake = _patch_requests(monkeypatch, [
        FakeResponse({"data": []}),
        FakeResponse({"id": 12, "book_id": 3, "slug": "vigil-w44", "name": NAME}),
        FakeResponse({"id": 3, "slug": "reports"}),
    ])

    result = _call({"name": NAME, "markdown": MARKDOWN})

    assert result == {
        "success": True,
        "action": "created",
        "page_id": 12,
        "url": "https://bks.example.com/books/reports/page/vigil-w44",
        "book_id": 3,
    }
    assert [call["method"] for call in fake.calls] == ["GET", "POST", "GET"]
    assert fake.calls[0]["params"]["filter[name]"] == NAME
    assert fake.calls[0]["headers"]["Authorization"] == "Token 1:supersecret"
    assert fake.calls[1]["json"] == {"name": NAME, "markdown": MARKDOWN, "book_id": 3}


def test_same_name_updates_instead_of_creating_duplicate(configured_env, monkeypatch):
    fake = _patch_requests(monkeypatch, [
        FakeResponse({"data": [
            {"id": 7, "book_id": 3, "name": NAME, "slug": "vigil-w44"},
            {"id": 8, "book_id": 4, "name": NAME, "slug": "other-book"},
        ]}),
        FakeResponse({"id": 7, "book_id": 3, "slug": "vigil-w44", "name": NAME}),
        FakeResponse({"id": 3, "slug": "reports"}),
    ])

    result = _call({"name": NAME, "markdown": MARKDOWN})

    assert result["action"] == "updated"
    assert result["page_id"] == 7
    assert [call["method"] for call in fake.calls] == ["GET", "PUT", "GET"]
    assert fake.calls[1]["url"].endswith("/api/pages/7")
    assert fake.calls[1]["json"] == {"name": NAME, "markdown": MARKDOWN}


def test_page_id_updates_directly(configured_env, monkeypatch):
    fake = _patch_requests(monkeypatch, [
        FakeResponse({"id": 9, "book_id": 3, "slug": "p9", "name": NAME}),
        FakeResponse({"id": 3, "slug": "reports"}),
    ])

    result = _call({"name": NAME, "markdown": MARKDOWN, "page_id": 9})

    assert result["action"] == "updated"
    assert result["page_id"] == 9
    assert fake.calls[0]["method"] == "PUT"
    assert fake.calls[0]["url"].endswith("/api/pages/9")


def test_missing_credentials_names_env_vars(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / "empty-home"))
    result = _call({"name": NAME, "markdown": MARKDOWN})
    error = result["error"]
    assert "BOOKSTACK_URL" in error
    assert "BOOKSTACK_TOKEN_ID" in error
    assert "BOOKSTACK_TOKEN_SECRET" in error


def test_credentials_file_requires_0600(clean_env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    cred = home / "credentials" / "bookstack.json"
    cred.parent.mkdir(parents=True)
    cred.write_text(json.dumps({
        "base_url": "https://file.example.com",
        "token_id": "2",
        "token_secret": "filesecret",
        "book_id": 5,
    }), encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(home))

    cred.chmod(0o644)
    too_open = _call({"name": NAME, "markdown": MARKDOWN})
    assert "权限过宽" in too_open["error"]
    assert "chmod 600" in too_open["error"]

    cred.chmod(0o600)
    fake = _patch_requests(monkeypatch, [
        FakeResponse({"data": []}),
        FakeResponse({"id": 21, "book_id": 5, "slug": "p21", "name": NAME}),
        FakeResponse({"id": 5, "slug": "reports"}),
    ])
    result = _call({"name": NAME, "markdown": MARKDOWN})
    assert result["action"] == "created"
    assert result["book_id"] == 5
    assert fake.calls[0]["headers"]["Authorization"] == "Token 2:filesecret"


def test_missing_book_id_is_explicit(configured_env, monkeypatch):
    configured_env.delenv("BOOKSTACK_BOOK_ID", raising=False)
    fake = _patch_requests(monkeypatch, [])
    result = _call({"name": NAME, "markdown": MARKDOWN})
    assert "建页需要 book_id" in result["error"]
    assert fake.calls == []


def test_chapter_id_can_create_without_book_id(configured_env, monkeypatch):
    configured_env.delenv("BOOKSTACK_BOOK_ID", raising=False)
    fake = _patch_requests(monkeypatch, [
        FakeResponse({"data": []}),
        FakeResponse({"id": 31, "book_id": 3, "slug": "p31", "name": NAME}),
        FakeResponse({"id": 3, "slug": "reports"}),
    ])
    result = _call({"name": NAME, "markdown": MARKDOWN, "chapter_id": 4})
    assert result["action"] == "created"
    assert fake.calls[1]["json"] == {"name": NAME, "markdown": MARKDOWN, "chapter_id": 4}


def test_error_never_echoes_token(configured_env, monkeypatch):
    secret = "supersecret"
    _patch_requests(monkeypatch, [
        FakeResponse(status_code=401, payload=None, text=f"invalid token 1:{secret}"),
    ])
    result = _call({"name": NAME, "markdown": MARKDOWN})
    payload = json.dumps(result, ensure_ascii=False)
    assert secret not in payload
    assert "***" in payload
    assert "BookStack API 401" in result["error"]


def test_timeout_must_be_positive(configured_env, monkeypatch):
    fake = _patch_requests(monkeypatch, [])
    result = _call({"name": NAME, "markdown": MARKDOWN, "timeout": 0})
    assert "timeout" in result["error"]
    assert fake.calls == []


def test_tool_is_registered_without_requires_env():
    from tools.registry import registry

    entry = registry.get_entry("publish_bookstack")
    assert entry is not None
    assert entry.toolset == "bookstack"
    assert list(entry.requires_env) == []
    assert entry.check_fn is None
    assert registry.get_schema("publish_bookstack")["name"] == "publish_bookstack"


def test_toolset_is_available_for_cron():
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset

    assert "publish_bookstack" in resolve_toolset("hermes-cron")
    assert "bookstack" in _get_platform_tools({}, "cron")


# ---------------------------------------------------------------------------
# publish_targets 抽象
# ---------------------------------------------------------------------------

def test_file_target_writes_and_updates(tmp_path):
    target = FileTarget(tmp_path)
    first = target.publish(name="../escape/周报", markdown="# 一\n")
    assert first["action"] == "created"
    assert Path(first["path"]) == tmp_path / "周报.md"
    assert Path(first["path"]).read_text(encoding="utf-8") == "# 一\n"

    second = target.publish(name="周报", markdown="# 二\n")
    assert second["action"] == "updated"
    assert Path(second["path"]).read_text(encoding="utf-8") == "# 二\n"


def test_confluence_target_is_reserved():
    target = ConfluenceTarget(base_url="https://wiki.example.com", token="x")
    with pytest.raises(NotImplementedError):
        target.publish(name=NAME, markdown=MARKDOWN)
    with pytest.raises(NotImplementedError):
        target._markdown_to_storage(MARKDOWN)


def test_build_target_rejects_unknown():
    with pytest.raises(PublishTargetError):
        build_target("wordpress")
