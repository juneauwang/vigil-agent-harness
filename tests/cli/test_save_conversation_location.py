"""Tests for /save — the conversation snapshot slash command.

Regression: the old implementation wrote ``hermes_conversation_<ts>.json``
to the current working directory (CWD). Users who ran /save expected the
file to be discoverable via ``vigil sessions browse``, but CWD-resident
snapshots are not indexed in the state DB and are generally invisible.
The fix writes snapshots under ``~/.vigil/sessions/saved/`` and prints
the absolute path plus the resume hint for the live session.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".vigil"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("VIGIL_HOME", str(home))
    return home


@pytest.fixture
def fresh_cli_module(hermes_home):
    """Re-import the top-level ``cli`` module against the fixture's VIGIL_HOME,
    then restore sys.modules exactly so the eviction cannot split module
    identity for later tests.

    Only ``cli`` and ``hermes_constants`` are reloaded. The previous broad
    ``startswith("cli")`` filter also evicted the entire ``hermes_cli`` package
    (``"hermes_cli".startswith("cli")`` is True) plus every submodule from
    sys.modules without restoring them — earlier-imported modules (e.g.
    ``hermes_cli.web_server``, ``hermes_cli.observability.relay_shared_metrics``)
    still held the original module objects while later imports resolved the
    fresh package, so per-test monkeypatches (profiles root, config caches)
    landed on the wrong copy and profile-scoped reads/writes silently fell back
    to the root install.
    """
    saved = {
        name: sys.modules[name]
        for name in ("cli", "hermes_constants")
        if name in sys.modules
    }
    try:
        for name in saved:
            sys.modules.pop(name, None)
        import cli  # noqa: F401  (module under test)
        yield cli
    finally:
        sys.modules.update(saved)


def _make_stub_cli(history):
    """Build a minimal object exposing just what save_conversation uses."""
    return SimpleNamespace(
        conversation_history=history,
        model="test-model",
        session_id="20260101_120000_abc123",
        session_start=datetime(2026, 1, 1, 12, 0, 0),
    )


def test_save_conversation_writes_under_hermes_home(
    hermes_home, fresh_cli_module, tmp_path, monkeypatch, capsys
):
    """Snapshot must land under ~/.vigil/sessions/saved/, not CWD."""
    # Change CWD to a different directory to prove the file does NOT go there.
    work = tmp_path / "somewhere-else"
    work.mkdir()
    monkeypatch.chdir(work)

    cli = fresh_cli_module
    stub = _make_stub_cli([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])

    # Call the unbound method against our stub.
    cli.HermesCLI.save_conversation(stub)

    # File must NOT be in CWD
    cwd_leak = list(work.glob("vigil_conversation_*.json"))
    assert not cwd_leak, f"snapshot leaked to CWD: {cwd_leak}"

    # File MUST be under ~/.vigil/sessions/saved/
    saved_dir = hermes_home / "sessions" / "saved"
    assert saved_dir.is_dir(), "expected saved/ subdirectory to be created"
    files = list(saved_dir.glob("vigil_conversation_*.json"))
    assert len(files) == 1, files

    payload = json.loads(files[0].read_text())
    assert payload["model"] == "test-model"
    assert payload["session_id"] == "20260101_120000_abc123"
    assert payload["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    # User-facing message must include the absolute path AND the resume hint.
    out = capsys.readouterr().out
    assert str(files[0]) in out, out
    assert "vigil --resume 20260101_120000_abc123" in out, out


def test_save_conversation_empty_history_does_nothing(hermes_home, fresh_cli_module, capsys):
    cli = fresh_cli_module
    stub = _make_stub_cli([])
    cli.HermesCLI.save_conversation(stub)

    saved_dir = hermes_home / "sessions" / "saved"
    assert not saved_dir.exists() or not list(saved_dir.iterdir())
    out = capsys.readouterr().out
    assert "No conversation to save" in out
