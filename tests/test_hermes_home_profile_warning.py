"""Tests for get_hermes_home() profile-mode resolution.

The legacy "VIGIL_HOME fallback" profile warning was removed in batch 11
(OPS-DELTA #43/#44 — Hermes compat deleted, data-root fallbacks dropped).
get_hermes_home() is now purely override → VIGIL_HOME → platform default:
an active_profile file has no effect on the resolved root, and there is
no warning to emit.
"""

from pathlib import Path

import pytest


@pytest.fixture
def fresh_constants(monkeypatch, tmp_path):
    """Import hermes_constants fresh and reset the one-shot warn flag."""
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    return hermes_constants


class TestGetHermesHomeProfileWarning:
    def test_classic_mode_no_active_profile_no_warning(
        self, fresh_constants, tmp_path, capsys
    ):
        """Classic mode: no active_profile file → silent, returns ~/.vigil."""
        result = fresh_constants.get_hermes_home()
        assert result == tmp_path / ".vigil"
        assert "VIGIL_HOME fallback" not in capsys.readouterr().err


    def test_named_profile_unset_home_resolves_silently(
        self, fresh_constants, tmp_path, capsys
    ):
        """active_profile=coder + VIGIL_HOME unset → plain ~/.vigil, silent."""
        hermes_dir = tmp_path / ".vigil"
        hermes_dir.mkdir()
        (hermes_dir / "active_profile").write_text("coder\n")

        result = fresh_constants.get_hermes_home()

        # Returns the platform default — no import-time crash.
        assert result == tmp_path / ".vigil"
        # No legacy fallback warning is emitted.
        err = capsys.readouterr().err
        assert "VIGIL_HOME fallback" not in err
        assert "'coder'" not in err

    def test_hermes_home_set_suppresses_warning(
        self, fresh_constants, tmp_path, capsys, monkeypatch
    ):
        """Even if active_profile is 'coder', setting VIGIL_HOME suppresses warning."""
        profile_dir = tmp_path / ".vigil" / "profiles" / "coder"
        profile_dir.mkdir(parents=True)
        (tmp_path / ".vigil" / "active_profile").write_text("coder\n")
        monkeypatch.setenv("VIGIL_HOME", str(profile_dir))

        result = fresh_constants.get_hermes_home()

        assert result == profile_dir
        assert "VIGIL_HOME fallback" not in capsys.readouterr().err

    def test_unreadable_active_profile_no_crash(
        self, fresh_constants, tmp_path, capsys
    ):
        """active_profile that can't be decoded → fall through silently."""
        hermes_dir = tmp_path / ".vigil"
        hermes_dir.mkdir()
        # Write bytes that aren't valid utf-8
        (hermes_dir / "active_profile").write_bytes(b"\xff\xfe\x00\x00")

        result = fresh_constants.get_hermes_home()

        assert result == tmp_path / ".vigil"
        # Shouldn't crash; shouldn't warn either (can't tell what profile was intended)
        assert "VIGIL_HOME fallback" not in capsys.readouterr().err
