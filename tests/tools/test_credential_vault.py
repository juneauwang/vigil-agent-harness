"""OPS-DELTA #16 输入侧 — 凭据保险箱（tools/credential_vault.py）。

验收点：
  - store 后文件权限 600、内容含值；retrieve 正确；expire 后不可用；
  - store/retrieve 分别登记 user/secret 来源（供 sudo guard 三态判定）；
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
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
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
    assert cv.has_credential_source("secret") is True


def test_store_registers_user_source(vault_home):
    cv.store("my_pass", "MyP@ssw0rd")
    assert cv.has_credential_source("user") is True
    assert cv.has_credential_source("secret") is False


def test_store_registers_value_in_global_redaction_registry(vault_home):
    """OPS-DELTA 批次三十二：store() 写入的值自动登记进全局打码登记表。"""
    from agent import redact
    redact._reset_registered_credential_values_for_tests()
    try:
        cv.store("srv_pass", "wwplove815")
        assert "wwplove815" in redact.registered_credential_values()
        # 登记后任意输出通道打码（低熵裸值形态）
        out = redact.redact_sensitive_text("echo 'wwplove815'", force=True)
        assert "wwplove815" not in out
        # 值本身不打日志
    finally:
        redact._reset_registered_credential_values_for_tests()


def test_expire_makes_retrieve_fail(vault_home):
    cv.store("tmp_secret", "TmpV@lue123456")
    assert cv.expire("tmp_secret") is True
    assert not _secret_path(vault_home, "tmp_secret").exists()
    with pytest.raises(FileNotFoundError):
        cv.retrieve("tmp_secret")
    assert cv.has_credential_source(("user", "secret")) is False


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
    entry = next(v for v in summary.values() if v["name"] == "secret_a")
    assert entry["source"] == "user"
    assert entry["session"]


def test_mark_user_authorized_registers_user_source(vault_home):
    assert cv.has_credential_source("user") is False
    cv.mark_user_authorized()
    assert cv.has_credential_source("user") is True


# ---------------------------------------------------------------------------
# A1（任务11审查）——来源登记按会话隔离，不再进程全局
# ---------------------------------------------------------------------------

def test_registered_summary_includes_session_dimension(vault_home):
    from tools.approval import reset_current_session_key, set_current_session_key

    tok = set_current_session_key("sess-sum")
    try:
        cv.store("secret_b", "AnotherValue456!")
    finally:
        reset_current_session_key(tok)
    summary = cv.registered_summary()
    entry = next(v for v in summary.values() if v["name"] == "secret_b")
    assert entry["session"] == "sess-sum"


def test_source_registry_is_session_scoped(vault_home):
    """会话 A 登记 → 会话 B 查不到；会话 A 自己查得到（A1 核心回归）。"""
    from tools.approval import reset_current_session_key, set_current_session_key

    tok_a = set_current_session_key("sess-A")
    cv.mark_user_authorized()
    assert cv.has_credential_source(("user", "secret")) is True

    tok_b = set_current_session_key("sess-B")
    try:
        assert cv.has_credential_source(("user", "secret")) is False
        assert cv.has_credential_source("user") is False
        assert cv.has_credential_source("secret") is False
    finally:
        reset_current_session_key(tok_b)

    # 回到会话 A：登记仍然有效（未被 B 的查询清除）
    assert cv.has_credential_source(("user", "secret")) is True
    reset_current_session_key(tok_a)


def test_sudo_stdin_guard_is_session_scoped(vault_home, monkeypatch):
    """sudo -S 防暴力守卫端到端：A 会话的用户确认只解除 A 的拦截。"""
    import tools.approval as approval_module
    from tools.approval import reset_current_session_key, set_current_session_key

    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    tok_a = set_current_session_key("sess-A")
    cv.mark_user_authorized()
    assert approval_module._check_sudo_stdin_guard(
        "sudo -S apt install nginx")[0] is False

    tok_b = set_current_session_key("sess-B")
    try:
        blocked, desc = approval_module._check_sudo_stdin_guard(
            "sudo -S apt install nginx")
        assert blocked is True
        assert desc and "guessing" in desc
    finally:
        reset_current_session_key(tok_b)

    reset_current_session_key(tok_a)


def test_expire_clears_all_sessions_marks(vault_home):
    """expire 清掉该凭据名在所有会话的登记（生命周期清理语义）。"""
    from tools.approval import reset_current_session_key, set_current_session_key

    tok_a = set_current_session_key("sess-A")
    cv.store("tmp_secret", "TmpV@lue123456")
    tok_b = set_current_session_key("sess-B")
    try:
        cv.store("tmp_secret", "TmpV@lue123456")
    finally:
        reset_current_session_key(tok_b)
    reset_current_session_key(tok_a)

    assert cv.expire("tmp_secret") is True
    tok_b2 = set_current_session_key("sess-B")
    try:
        assert cv.has_credential_source(("user", "secret")) is False
    finally:
        reset_current_session_key(tok_b2)
    assert cv.has_credential_source(("user", "secret")) is False


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
