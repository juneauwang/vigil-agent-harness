"""Startup hint for a failed tirith install must be dim and non-alarming.

Regression: every CLI launch printed
    ⚠ tirith security scanner enabled but not available — ...
even while the background auto-install was still downloading (first run),
scaring users on restricted networks where the download can take seconds or
never complete. The hint is now gated on ``tirith_install_status()``:
only a genuine install failure (disk marker / in-memory sentinel) prints a
single dim line; in-flight downloads and unsupported platforms stay silent.
"""

from unittest.mock import patch

from cli import HermesCLI


def _make_cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {}
    cli._tirith_security_checked = False
    return cli


class TestTirithStartupHint:
    def test_install_in_flight_is_silent(self):
        cli = _make_cli()
        with patch("cli._cprint") as cprint, \
             patch("tools.tirith_security.ensure_installed", return_value=None), \
             patch("tools.tirith_security.tirith_install_status", return_value="installing"):
            cli._ensure_tirith_security()
        cprint.assert_not_called()

    def test_unsupported_platform_is_silent(self):
        cli = _make_cli()
        with patch("cli._cprint") as cprint, \
             patch("tools.tirith_security.ensure_installed", return_value=None), \
             patch("tools.tirith_security.tirith_install_status", return_value="unsupported"):
            cli._ensure_tirith_security()
        cprint.assert_not_called()

    def test_disabled_is_silent(self):
        cli = _make_cli()
        with patch("cli._cprint") as cprint, \
             patch("tools.tirith_security.ensure_installed", return_value=None), \
             patch("tools.tirith_security.tirith_install_status", return_value="disabled"):
            cli._ensure_tirith_security()
        cprint.assert_not_called()

    def test_failed_prints_dim_hint_without_warning_glyph(self):
        cli = _make_cli()
        with patch("cli._cprint") as cprint, \
             patch("tools.tirith_security.ensure_installed", return_value=None), \
             patch("tools.tirith_security.tirith_install_status", return_value="failed"):
            cli._ensure_tirith_security()
        cprint.assert_called_once()
        text = cprint.call_args.args[0]
        assert "tirith scanner unavailable" in text
        assert "built-in patterns" in text
        assert "⚠" not in text

    def test_checked_flag_prevents_repeat(self):
        cli = _make_cli()
        with patch("cli._cprint") as cprint, \
             patch("tools.tirith_security.ensure_installed", return_value=None), \
             patch("tools.tirith_security.tirith_install_status", return_value="failed"):
            cli._ensure_tirith_security()
            cli._ensure_tirith_security()
        cprint.assert_called_once()
