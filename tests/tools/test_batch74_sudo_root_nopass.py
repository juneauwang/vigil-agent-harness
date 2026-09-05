"""batch74（OPS-DELTA #89）任务 2：远端 sudo root 免密 + 非 root 兜底验收测试。

覆盖（任务书 §任务 2）：
  - ssh_key + root → 远端命令是 ``sudo -n <command>``，无 askpass/secret 注入；
  - ssh_key + root + sudo 仍要求密码（stderr 密码提示）→ 可操作错误含"配置 secret"；
  - vault 类型 → 现有 ``sudo -A`` scp askpass 路径不变；
  - ssh_key + 非 root + ``sudo -n`` 成功 → 直通；
  - ssh_key + 非 root + ``sudo -n`` 密码提示 → 引导配 vault；
  - ssh_key + 命令自身失败（无密码提示）→ 直通返回命令结果（不误报需密码）；
  - askpass 类型仍 fail-closed。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tools import sudo_tool
from tools.sudo_tool import _run_remote_sudo


class FakeSSHRunner:
    """mock ``_ssh_run``：记录 ssh argv/env 与远端命令，按应答表返回结果。"""

    def __init__(self):
        self.calls = []
        self.responses = {}  # key: remote_cmd 前缀 → (returncode, stdout, stderr)

    def __call__(self, ssh_argv, ssh_env, remote_cmd, timeout=120):
        self.calls.append({"argv": ssh_argv, "env": ssh_env, "cmd": remote_cmd})
        for prefix, resp in self.responses.items():
            if remote_cmd.startswith(prefix):
                return SimpleNamespace(returncode=resp[0], stdout=resp[1], stderr=resp[2])
        raise AssertionError(f"未预期的远端命令: {remote_cmd!r}")


@pytest.fixture
def fake_ssh(monkeypatch):
    runner = FakeSSHRunner()
    monkeypatch.setattr(sudo_tool, "_ssh_run", runner)
    monkeypatch.setattr(
        sudo_tool, "_build_ssh_argv",
        lambda host, **kw: (["ssh", "-o", "IdentitiesOnly=yes", "-p", str(kw.get("port", 22)),
                             f"{kw.get('user', 'root')}@{host}"], {}),
    )
    return runner


def _ssh_key_cred(**overrides):
    cred = {"type": "ssh_key", "ref": "/keys/aliyun_nopass.pem", "user": "root", "port": 22}
    cred.update(overrides)
    return cred


class TestRemoteSudoRootNopass:
    def test_root_ssh_key_sudo_n_direct(self, fake_ssh):
        """root + ssh_key → ``sudo -n <command>`` 直通，无 askpass/secret 注入。"""
        fake_ssh.responses["sudo -n"] = (0, "ok", "")
        result = _run_remote_sudo("10.0.0.1", "root", 22, "kubectl get pods", _ssh_key_cred())
        assert result.returncode == 0
        assert fake_ssh.calls == [{
            "argv": ["ssh", "-o", "IdentitiesOnly=yes", "-p", "22", "root@10.0.0.1"],
            "env": {},
            "cmd": "sudo -n kubectl get pods",
        }]
        blob = str(fake_ssh.calls)
        assert "sudo -A" not in blob and "SUDO_ASKPASS" not in blob
        assert "sudo -S" not in blob

    def test_root_ssh_key_password_prompt_guides_vault(self, fake_ssh):
        """root + ssh_key 但 sudo 仍要求密码 → 可操作错误，引导配置 secret。"""
        fake_ssh.responses["sudo -n"] = (1, "", "sudo: a password is required")
        with pytest.raises(RuntimeError, match="配置 secret"):
            _run_remote_sudo("10.0.0.1", "root", 22, "ss -tlnp", _ssh_key_cred())

    def test_root_ssh_key_command_failure_no_password_hint_returns(self, fake_ssh):
        """root + ssh_key：命令自身失败（无密码提示）→ 直通返回结果，不误报需密码。"""
        fake_ssh.responses["sudo -n"] = (2, "", "cat: /nonexistent: No such file")
        result = _run_remote_sudo("10.0.0.1", "root", 22, "cat /nonexistent", _ssh_key_cred())
        assert result.returncode == 2

    def test_vault_path_unchanged(self, tmp_path, monkeypatch):
        """secret 类型 → 现有 ``sudo -A`` scp askpass 路径不变。"""
        vault = tmp_path / "srv-pass"
        vault.write_text("pw", encoding="utf-8")
        monkeypatch.setattr(sudo_tool, "path_for", lambda name: vault)
        cred = {"type": "secret", "ref": "srv-pass", "user": "ops", "port": 22}
        calls = {"sudo": None}
        monkeypatch.setattr(
            sudo_tool, "_build_ssh_argv",
            lambda host, **kw: (["ssh", "-p", "22", "ops@host"], {}),
        )

        def fake_run(argv, **kwargs):
            if argv and argv[0] == "scp":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv and argv[0] == "ssh":
                cmd = argv[-1]
                if "sudo -A" in cmd:
                    calls["sudo"] = cmd
                    return SimpleNamespace(returncode=0, stdout="ok", stderr="")
                if "rm -f" in cmd or "chmod" in cmd:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(argv)

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        result = _run_remote_sudo("host", "ops", 22, "ss -tlnp", cred)
        assert result.returncode == 0
        assert re.search(r"SUDO_ASKPASS=/tmp/vigil-sudo-[^ ]+\.sh sudo -A ss -tlnp$",
                         calls["sudo"])

    def test_non_root_ssh_key_sudo_n_success_direct(self, fake_ssh):
        """非 root + ssh_key + NOPASSWD → ``sudo -n`` 成功直通。"""
        fake_ssh.responses["sudo -n"] = (0, "ok", "")
        result = _run_remote_sudo("10.0.0.1", "ops", 22, "systemctl status nginx",
                                  _ssh_key_cred(user="ops"))
        assert result.returncode == 0
        assert fake_ssh.calls[-1]["cmd"] == "sudo -n systemctl status nginx"

    def test_non_root_ssh_key_sudo_n_failure_guides_vault(self, fake_ssh):
        """非 root + ssh_key：sudo 要求密码 → 引导配置 secret（保持现状语义）。"""
        fake_ssh.responses["sudo -n"] = (1, "", "sudo: a password is required")
        with pytest.raises(RuntimeError, match="配置 secret"):
            _run_remote_sudo("10.0.0.1", "ops", 22, "systemctl restart nginx",
                             _ssh_key_cred(user="ops"))

    def test_askpass_still_fail_closed(self, fake_ssh):
        """askpass 类型远端注入仍不支持 → fail-closed。"""
        with pytest.raises(RuntimeError, match="secret 类型"):
            _run_remote_sudo("host", "root", 22, "ls",
                             {"type": "askpass", "ref": "/keys/x"})
