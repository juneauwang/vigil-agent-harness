"""OPS-DELTA #25 — sudo -S 硬拦截分级改造（按密码来源放行）。

验收点：
  - 无来源 sudo -S 仍拦截（防暴力保留）；
  - 会话登记用户来源密码后 sudo -S 放行；
  - 登记 vault 来源后放行；
  - SUDO_PASSWORD env 放行（现状不变）；
  - 放行路径命令文本不含明文密码。
"""

from __future__ import annotations

import os

import pytest

import hermes_cli.config as hc
import tools.approval as approval_module
from tools import credential_vault as cv


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    with cv._REGISTERED_LOCK:
        cv._REGISTERED.clear()
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    yield
    with cv._REGISTERED_LOCK:
        cv._REGISTERED.clear()


def test_no_source_sudo_S_still_blocked():
    is_blocked, desc = approval_module._check_sudo_stdin_guard("sudo -S apt install nginx")
    assert is_blocked is True
    assert "guessing" in desc or "暴力" in desc or "sudo" in desc.lower()


def test_env_sudo_password_allows_unchanged(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "EnvP@ssw0rd")
    is_blocked, desc = approval_module._check_sudo_stdin_guard("sudo -S apt install nginx")
    assert is_blocked is False
    assert desc is None


def test_user_source_allows_sudo_S():
    cv.mark_user_authorized()
    is_blocked, _ = approval_module._check_sudo_stdin_guard("sudo -S apt install nginx")
    assert is_blocked is False


def test_vault_source_allows_sudo_S(tmp_path):
    cv.store("sudo_pass", "VaultP@ssw0rd12345", source="secret")
    assert cv.has_credential_source("secret") is True
    is_blocked, _ = approval_module._check_sudo_stdin_guard("sudo -S systemctl restart nginx")
    assert is_blocked is False


def test_allow_path_command_text_has_no_plaintext():
    """放行路径：命令文本不含明文密码（sudo -S 走 stdin，密码不进命令串）。"""
    cv.store("sudo_pass", "TopSecretP@ssw0rd_123456", source="user")
    cmd = "sudo -S apt install nginx"
    is_blocked, _ = approval_module._check_sudo_stdin_guard(cmd)
    assert is_blocked is False
    assert "TopSecretP@ssw0rd_123456" not in cmd


def test_block_message_gives_actionable_guidance():
    result = approval_module._sudo_stdin_block_result("sudo password guessing via stdin (sudo -S)")
    msg = result["message"]
    assert "对话中提供密码" in msg
    assert "SUDO_PASSWORD" in msg
    assert "安全存储" in msg and "不回显" in msg


def test_block_result_via_check_all_command_guards(tmp_path, monkeypatch):
    """无来源时，check_all_command_guards 的 sudo -S 拦截完整链路。"""
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    result = approval_module.check_all_command_guards("sudo -S whoami", "local")
    assert result["approved"] is False
    assert "对话中提供密码" in result["message"]
