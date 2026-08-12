"""OPS-DELTA #16 输入侧 — 凭据保险箱（tools/credential_vault.py）。

验收点：
  - store 后文件权限 600、内容含值；retrieve 正确；expire 后不可用；
  - store/retrieve 分别登记 user/vault 来源（供 sudo guard 三态判定）；
  - 名非法（路径穿越）拒绝；owner 校验存在。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import hermes_cli.config as hc
from tools import credential_vault as cv


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    with cv._REGISTERED_LOCK:
        cv._REGISTERED.clear()
    yield tmp_path
    with cv._REGISTERED_LOCK:
        cv._REGISTERED.clear()


def _secret_path(home: Path, name: str) -> Path:
    return home / "secrets" / name


def test_store_writes_0600_with_value(vault_home):
    cv.store("grafana_admin", "Sup3rS3cretP@ss", source="user")
    path = _secret_path(vault_home, "grafana_admin")
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "Sup3rS3cretP@ss"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_retrieve_returns_value_and_registers_vault_source(vault_home):
    cv.store("db_pass", "DbP@ssw0rd12345")
    assert cv.retrieve("db_pass") == "DbP@ssw0rd12345"
    assert cv.has_credential_source("vault") is True


def test_store_registers_user_source(vault_home):
    cv.store("my_pass", "MyP@ssw0rd")
    assert cv.has_credential_source("user") is True
    assert cv.has_credential_source("vault") is False


def test_expire_makes_retrieve_fail(vault_home):
    cv.store("tmp_secret", "TmpV@lue123456")
    assert cv.expire("tmp_secret") is True
    assert not _secret_path(vault_home, "tmp_secret").exists()
    with pytest.raises(FileNotFoundError):
        cv.retrieve("tmp_secret")
    assert cv.has_credential_source(("user", "vault")) is False


def test_invalid_name_rejected(vault_home):
    with pytest.raises(ValueError):
        cv.store("../escape", "x")
    with pytest.raises(ValueError):
        cv.retrieve("a/b")
    with pytest.raises(ValueError):
        cv.expire("")


def test_registered_summary_has_no_plaintext(vault_home):
    cv.store("secret_a", "PlainTextValue123!")
    summary = cv.registered_summary()
    assert "PlainTextValue123!" not in str(summary)
    assert summary["secret_a"]["source"] == "user"


def test_mark_user_authorized_registers_user_source(vault_home):
    assert cv.has_credential_source("user") is False
    cv.mark_user_authorized()
    assert cv.has_credential_source("user") is True


def test_owner_check_blocks_other_owner(vault_home, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("POSIX only")
    cv.store("owned", "SomeValue123!")
    path = _secret_path(vault_home, "owned")
    # 模拟属主不是当前用户 → 读取被拒
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(PermissionError):
        cv.retrieve("owned")
