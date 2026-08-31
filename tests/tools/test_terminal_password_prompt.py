"""批次十九 §AG — ansible -K / ssh passphrase 交互密码提示转 clarify。

terminal 工具在非 TTY 环境把密码提示当普通输出透传 → 进程挂起等输入直到
超时，getpass 还可能把密码回显进会话记录。挂载点：terminal_tool 前景执行
路径的 output 后处理——输出以密码提示收尾（进程在等输入）→ 不透传，返回
明确错误引导 clarify / ANSIBLE_BECOME_PASS / vssh/sudo_exec。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from tools.terminal_tool import _interactive_password_prompt_hint


# ---------------------------------------------------------------------------
# 识别层：_interactive_password_prompt_hint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("output", [
    "BECOME password:",
    "BECOME password:\n[Command timed out after 120s]",
    "[sudo] password for user:",
    "Password for 'root'@'localhost':",
    "Enter passphrase for key '/home/u/.ssh/id_ed25519':",
    "ansible-playbook 2.14.1\nBECOME password: ",
    "Password: ",
    "sudo password:",
])
def test_password_prompt_tail_detected(output):
    assert _interactive_password_prompt_hint(output) is not None, output


@pytest.mark.parametrize("output", [
    "",
    "ok",
    "All tasks passed.",
    "password",
    "grep result: password",
    "note: password is set",
    "task completed with exit 0\npassword stored in vault",
])
def test_no_password_prompt_not_detected(output):
    assert _interactive_password_prompt_hint(output) is None, output


def test_password_mention_mid_output_not_detected():
    """长输出中间提到密码、以普通文本收尾 → 不误伤。"""
    out = ("playbook started\nBECOME password prompt appears mid-run\n"
           "PLAY RECAP: ok=3 changed=0")
    assert _interactive_password_prompt_hint(out) is None


# ---------------------------------------------------------------------------
# 转换层：terminal_tool 前景路径返回 clarify 错误（不挂起透传）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_env_cache():
    """每个测试前清空 _active_environments 缓存（task_id 会折叠到 default）。"""
    import tools.terminal_tool as tt
    tt._active_environments.clear()
    tt._creation_locks.clear()
    yield
    tt._active_environments.clear()
    tt._creation_locks.clear()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    # batch83-fix（OPS-DELTA #99）：矩阵缺失 → deny（fail-closed）。本套件测
    # terminal 密码提示转 clarify，与权限矩阵无关——显式关闭 ops 权限，避免
    # terminal 路径被"矩阵未初始化"提前拦截。
    (tmp_path / ".vigil").mkdir(exist_ok=True)
    (tmp_path / ".vigil" / "config.yaml").write_text(
        "ops:\n  permissions:\n    enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / ".vigil"))
    return tmp_path


def _make_env_config(**overrides):
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": "/tmp",
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
    }
    config.update(overrides)
    return config


def test_ansible_bk_prompt_converts_to_clarify_error(isolated_home, monkeypatch):
    """ansible-playbook -bK 的 BECOME password 提示 → 明确错误（不挂起）。"""
    import tools.terminal_tool as tt
    from tools.terminal_tool import terminal_tool

    fake_env = MagicMock()
    fake_env.execute.return_value = {
        "output": "BECOME password:\n[Command timed out after 120s]",
        "returncode": 124,
    }
    fake_env.cwd = "/tmp"
    monkeypatch.setattr(tt, "_get_env_config", lambda: _make_env_config())
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_create_environment", lambda **kw: fake_env)
    # YAPL P2（OPS-DELTA #68）的 ansible -i inventory 硬拦（fail-closed）在
    # 密码提示转换之前触发，属于独立安全层（见 test_ansible_inventory.py）。
    # 这里 mock 放行，只验证本测试关心的 BECOME password → clarify 转换。
    monkeypatch.setattr(
        "tools.ansible_inventory_guard.check_ansible_inventory_guard",
        lambda command, home=None: None,
    )

    result = json.loads(terminal_tool(
        "ansible-playbook -i hosts -bK play.yml", task_id="t-pw-1"))

    assert result.get("error"), result
    assert "交互式密码提示" in result["error"]
    assert "ANSIBLE_BECOME_PASS" in result["error"]
    assert "clarify" in result["error"]
    assert "BECOME password" in result["error"]
    assert result["exit_code"] == 124


def test_ssh_passphrase_prompt_converts_to_clarify_error(isolated_home, monkeypatch):
    """ssh 加密私钥 passphrase 提示同样转 clarify（getpass 回显防护）。"""
    import tools.terminal_tool as tt
    from tools.terminal_tool import terminal_tool

    fake_env = MagicMock()
    fake_env.execute.return_value = {
        "output": "Enter passphrase for key '/home/u/.ssh/id_ed25519':",
        "returncode": 124,
    }
    fake_env.cwd = "/tmp"
    monkeypatch.setattr(tt, "_get_env_config", lambda: _make_env_config())
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_create_environment", lambda **kw: fake_env)

    result = json.loads(terminal_tool(
        "ssh -i /home/u/.ssh/id_ed25519 db1 uptime", task_id="t-pw-2"))

    assert result.get("error") and "交互式密码提示" in result["error"]
    assert "Enter passphrase" in result["error"]


def test_plain_output_passes_through(isolated_home, monkeypatch):
    """普通命令输出不含密码提示 → 原样透传（exit 0 正常结果）。"""
    import tools.terminal_tool as tt
    from tools.terminal_tool import terminal_tool

    fake_env = MagicMock()
    fake_env.execute.return_value = {
        "output": "ok: run 3 tasks",
        "returncode": 0,
    }
    fake_env.cwd = "/tmp"
    monkeypatch.setattr(tt, "_get_env_config", lambda: _make_env_config())
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_create_environment", lambda **kw: fake_env)

    result = json.loads(terminal_tool("ansible-playbook play.yml", task_id="t-pw-3"))

    assert result.get("error") is None, result
    assert result["output"] == "ok: run 3 tasks"
    assert result["exit_code"] == 0
