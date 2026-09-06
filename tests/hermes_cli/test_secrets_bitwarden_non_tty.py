"""Regression tests for vigil secrets bitwarden setup non-TTY guard.

Issue #40274: cmd_setup() crashes with EOFError when stdin is not a TTY
because getpass.getpass() and console.input() require an interactive terminal.
"""
from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest


class TestCmdSetupNonTtyGuard:
    """cmd_setup should fail early with a clear error in non-TTY environments."""

    @staticmethod
    def _make_args(**overrides):
        ns = argparse.Namespace(
            access_token=overrides.get("access_token", ""),
            server_url=overrides.get("server_url", ""),
            project_id=overrides.get("project_id", ""),
        )
        return ns


    def test_missing_access_token_only(self, monkeypatch, capsys):
        """Non-TTY with server-url and project-id but no token → reports --access-token."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )

        from hermes_cli.secrets_cli import cmd_setup

        result = cmd_setup(self._make_args(
            server_url="https://vault.bitwarden.com",
            project_id="aaaa-bbbb",
        ))
        assert result == 1
        captured = capsys.readouterr()
        # The "Missing:" line should list --access-token only
        assert "Missing:" in captured.out
        assert "--access-token" in captured.out
        # The usage example contains --server-url and --project-id, so check
        # the missing line specifically: it should NOT list them as missing
        missing_line = [l for l in captured.out.split("\n") if "Missing:" in l][0]
        assert "--access-token" in missing_line
        assert "--server-url" not in missing_line
        assert "--project-id" not in missing_line

    def test_missing_server_url_with_env_var_passes(self, monkeypatch):
        """Non-TTY with BWS_SERVER_URL env set → server-url not required."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BWS_SERVER_URL", "https://vault.bitwarden.com")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli.load_config", lambda: {})
        monkeypatch.setattr("hermes_cli.secrets_cli.save_env_value", lambda *a: None)
        monkeypatch.setattr("hermes_cli.secrets_cli.get_env_path", lambda: "/tmp/.env")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.fetch_bitwarden_secrets",
            lambda **kw: ({"KEY": "val"}, []),
        )

        from hermes_cli.secrets_cli import cmd_setup

        result = cmd_setup(self._make_args(
            access_token="0.valid-token",
            project_id="aaaa-bbbb",
        ))
        assert result == 0




# ---------------------------------------------------------------------------
# F1（任务15）—— token env var 可满足非 TTY setup（推荐脚本路径，argv 不再有 secret）
# ---------------------------------------------------------------------------

class TestCmdSetupEnvTokenFallback:
    def test_env_token_satisfies_non_tty_setup(self, monkeypatch, tmp_path):
        """非 TTY + 无 --access-token + BWS_ACCESS_TOKEN 已设 → setup 成功并落盘。"""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.env-token-placeholder")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0")
        saved: dict = {}
        monkeypatch.setattr("hermes_cli.secrets_cli.load_config", lambda: {})
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.save_env_value",
            lambda var, val: saved.setdefault(var, val),
        )
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.get_env_path", lambda: str(tmp_path / ".env")
        )
        monkeypatch.setattr("hermes_cli.secrets_cli.save_config", lambda cfg: None)
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.fetch_bitwarden_secrets",
            lambda **kw: ({"KEY": "val"}, []),
        )

        from hermes_cli.secrets_cli import cmd_setup

        ns = argparse.Namespace(
            access_token="",
            server_url="https://vault.bitwarden.com",
            project_id="aaaa-bbbb",
        )
        result = cmd_setup(ns)
        assert result == 0
        # env 中的 token 被持久化（setup 语义 = 存储）
        assert saved.get("BWS_ACCESS_TOKEN") == "0.env-token-placeholder"

    def test_env_token_shown_as_satisfied_in_missing_guard(self, monkeypatch, capsys):
        """非 TTY + env token 已设 + 缺 project_id → Missing 行不再列 --access-token。"""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.env-token-placeholder")
        monkeypatch.setenv("BWS_SERVER_URL", "https://vault.bitwarden.com")
        monkeypatch.setattr(
            "hermes_cli.secrets_cli.bw.find_bws", lambda install_if_missing=False: "/usr/bin/bws"
        )
        monkeypatch.setattr("hermes_cli.secrets_cli._bws_version", lambda _: "2.0.0")

        from hermes_cli.secrets_cli import cmd_setup

        ns = argparse.Namespace(access_token="", server_url="", project_id="")
        result = cmd_setup(ns)
        assert result == 1
        captured = capsys.readouterr()
        missing_line = [l for l in captured.out.split("\n") if "Missing:" in l][0]
        assert "--access-token" not in missing_line
        assert "--project-id" in missing_line
