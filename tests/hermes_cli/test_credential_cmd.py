"""``vigil credential`` 最小测试（store/list/expire，不打印明文）。"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

from hermes_cli.credential_cmd import cmd_credential


def _args(**kwargs):
    kwargs.setdefault("credential_command", None)
    return SimpleNamespace(**kwargs)


def test_credential_store_list_expire(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO("token-id:token-secret\n"))

    assert cmd_credential(_args(credential_command="store", name="bookstack-api", stdin=True)) == 0
    out = capsys.readouterr().out
    assert "stored: bookstack-api" in out
    assert "token-secret" not in out

    assert cmd_credential(_args(credential_command="list")) == 0
    assert "bookstack-api" in capsys.readouterr().out

    assert cmd_credential(_args(credential_command="expire", name="bookstack-api")) == 0
    assert "expired: bookstack-api" in capsys.readouterr().out
    assert cmd_credential(_args(credential_command="list")) == 0
    assert "vault is empty" in capsys.readouterr().out
