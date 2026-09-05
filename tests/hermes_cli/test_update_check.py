"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest




def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """When cache is fresh, check_for_updates should return cached value without calling git."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    # Create a fake git repo and fresh cache
    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    # The non-main checkout guard (branch guard) is pinned to "main" here so
    # this test isolates the cache contract; the guard itself is covered by
    # test_non_main_branch_skips_update_check below.
    with patch("hermes_cli.banner._checkout_branch", return_value="main"):
        with patch("hermes_cli.banner.subprocess.run") as mock_run:
            result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()


def test_non_main_branch_skips_update_check(tmp_path, monkeypatch):
    """A checkout on a non-main branch (e.g. the v1.0 release line) never
    follows origin/main — the behind count is bogus, so the check returns
    None (no banner / no badge) even when a stale cache entry exists."""
    from hermes_cli.banner import check_for_updates
    from hermes_cli import __version__

    repo_dir = tmp_path / "hermes-agent"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / ".git" / "HEAD").write_text("ref: refs/heads/v1.0\n", encoding="utf-8")

    # Stale cache written by the pre-guard check must NOT resurrect the bogus
    # behind count for a non-main checkout.
    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({"ts": time.time(), "behind": 3, "ver": __version__}))

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    # Pin the branch reader to a non-main branch (the running-code repo's
    # real branch is irrelevant to this contract).
    with patch("hermes_cli.banner._checkout_branch", return_value="v1.0"):
        with patch("hermes_cli.banner.subprocess.run") as mock_run:
            result = check_for_updates()

    assert result is None
    mock_run.assert_not_called()  # guard short-circuits before any git IO






def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    # Reset module state
    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        # Should return almost immediately (well under 1 second)
        assert elapsed < 1.0

        # Wait for the background thread to finish
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5




