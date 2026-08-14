"""批次十六 §W — ``sudo_exec`` agent 提权工具验收测试（OPS-DELTA 批次十六 任务 2）。

覆盖：注入走 ASKPASS（``sudo -A`` + SUDO_ASKPASS env，**不是** ``sudo -S <<<``）、
askpass 脚本只 cat 保险箱文件、argv/env/命令串无密码明文；权限矩阵联动
（prod 变更类 → require_confirmation、deny → 拒绝、只读 → 直接执行）；
安全边界（``>`` / ``bash -c`` 被拒）；凭据缺失 fail-closed。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import sudo_tool
from tools.sudo_tool import _sudo_exec_handler, _run_local_sudo, _run_remote_sudo

SECRET = "sup3r-s3cr3t-42!"


def _vault_cred(tmp_path, monkeypatch):
    """vault 类型凭据 + 临时保险箱文件（mock path_for 指向临时文件）。"""
    vault = tmp_path / "secrets" / "srv-pass"
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_text(SECRET, encoding="utf-8")
    monkeypatch.setattr(sudo_tool, "path_for", lambda name: vault)
    return {"type": "vault", "ref": "srv-pass", "user": "ops", "port": 22}


class TestLocalSudoAskpassInjection:
    def test_local_sudo_A_askpass_no_plaintext(self, tmp_path, monkeypatch):
        """本地 sudo：argv 是 ``sudo -A <command>``，密码只经 SUDO_ASKPASS 注入。"""
        cred = _vault_cred(tmp_path, monkeypatch)
        calls = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            askpass = Path(kwargs["env"]["SUDO_ASKPASS"])
            calls["askpass_content"] = askpass.read_text(encoding="utf-8")
            calls["askpass_mode"] = askpass.stat().st_mode & 0o777
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        result = _run_local_sudo("ss -tlnp", cred)

        assert result.returncode == 0
        assert calls["argv"] == ["sudo", "-A", "ss", "-tlnp"]
        assert "SUDO_ASKPASS" in calls["kwargs"]["env"]
        # askpass 脚本只 cat 保险箱文件（0700）
        assert calls["askpass_content"] == f"#!/bin/sh\ncat {tmp_path / 'secrets' / 'srv-pass'}\n"
        assert calls["askpass_mode"] == 0o700
        # 明文不进 argv / env / 命令串
        blob = json.dumps(calls["argv"]) + json.dumps(calls["kwargs"].get("env", {}))
        assert SECRET not in blob
        assert "sudo -S" not in " ".join(calls["argv"])

    def test_local_sudo_askpass_cred_type_uses_ref_directly(self, tmp_path, monkeypatch):
        """askpass 类型凭据：ref 脚本直接作为 SUDO_ASKPASS。"""
        script = tmp_path / "ask.sh"
        script.write_text("#!/bin/sh\ncat /vault\n", encoding="utf-8")
        os_chmod = __import__("os").chmod
        os_chmod(script, 0o700)
        calls = {}

        def fake_run(argv, **kwargs):
            calls["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        _run_local_sudo("vmstat 1 2", {"type": "askpass", "ref": str(script)})
        assert calls["kwargs"]["env"]["SUDO_ASKPASS"] == str(script)

    def test_local_ssh_key_credential_fail_closed(self, tmp_path, monkeypatch):
        """ssh_key 无密码明文 → fail-closed，不猜测。"""
        with pytest.raises(RuntimeError, match="vault/askpass"):
            _run_local_sudo("ss -tlnp", {"type": "ssh_key", "ref": "/keys/x.pem"})


class TestRemoteSudoAskpassInjection:
    def test_remote_sudo_A_with_remote_askpass_no_plaintext(self, tmp_path, monkeypatch):
        """远端 sudo：命令形态 ``SUDO_ASKPASS=<远端脚本> sudo -A <command>``，
        明文不过 argv/env/命令串；scp 传脚本+保险箱文件，结束后清理。"""
        cred = _vault_cred(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sudo_tool, "_build_ssh_argv",
            lambda host, **kw: (["ssh", "-p", "22", "ops@host"], {}),
        )
        calls = {"sudo": None, "cleanup": None}

        def fake_run(argv, **kwargs):
            if argv and argv[0] == "scp":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv and argv[0] == "ssh":
                cmd = argv[-1]
                if "sudo -A" in cmd:
                    calls["sudo"] = {"cmd": cmd, "kwargs": kwargs}
                    return SimpleNamespace(returncode=0, stdout="ok", stderr="")
                if "rm -f" in cmd:
                    calls["cleanup"] = cmd
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if "chmod" in cmd:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(argv)

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        result = _run_remote_sudo("host", "ops", 22, "ss -tlnp", cred)

        assert result.returncode == 0
        assert calls["sudo"] is not None
        # 命令形态：sudo -A + SUDO_ASKPASS env（不是 sudo -S <<<）
        assert re.search(r"SUDO_ASKPASS=/tmp/vigil-sudo-[^ ]+\.sh sudo -A ss -tlnp$",
                         calls["sudo"]["cmd"])
        assert "sudo -S" not in calls["sudo"]["cmd"]
        # 明文不过 ssh 命令串 / env / argv
        blob = json.dumps(calls["sudo"]["cmd"]) + json.dumps(calls["sudo"]["kwargs"].get("env", {}))
        assert SECRET not in blob
        # 远端临时凭据清理执行
        assert calls["cleanup"] is not None
        assert "/tmp/vigil-sudo-" in calls["cleanup"]

    def test_remote_non_vault_credential_fail_closed(self, tmp_path, monkeypatch):
        """ssh_key / askpass 类型不支持远端注入 → fail-closed。"""
        with pytest.raises(RuntimeError, match="vault 类型"):
            _run_remote_sudo("host", "ops", 22, "ss -tlnp",
                             {"type": "ssh_key", "ref": "/keys/x.pem"})


class TestHandlerPermissionMatrix:
    def test_prod_change_requires_confirmation(self, monkeypatch):
        """prod 变更类 → require_confirmation（人工确认门），不执行。"""
        monkeypatch.setattr(
            sudo_tool, "check_ops_command_permission",
            lambda command, target_env=None: {
                "action": "approve", "grade": "L2", "env": "prod",
                "env_tier": "prod", "role": "prod", "require_confirmation": True,
                "description": "⚠ prod 变更确认门：命令分级 L2 在 prod 环境需审批",
            },
        )
        executed = []
        monkeypatch.setattr(sudo_tool, "_run_remote_sudo",
                            lambda *a, **k: executed.append(a) or SimpleNamespace(
                                returncode=0, stdout="", stderr=""))
        out = json.loads(_sudo_exec_handler(
            {"host": "prod1", "command": "systemctl restart nginx", "env": "prod"}))
        assert out["status"] == "require_confirmation"
        assert out["action"] == "approve"
        assert out["require_confirmation"] is True
        assert "systemctl restart nginx" in out["command"]
        assert executed == []  # 未执行

    def test_deny_returns_tool_error(self, monkeypatch):
        monkeypatch.setattr(
            sudo_tool, "check_ops_command_permission",
            lambda command, target_env=None: {
                "action": "deny", "grade": "L4", "env": "prod",
                "require_confirmation": False, "description": "L4 拒绝",
            },
        )
        out = _sudo_exec_handler(
            {"host": "prod1", "command": "rm -rf /data", "env": "prod"})
        assert "拒绝" in out

    def test_readonly_command_executes_without_confirmation(self, tmp_path, monkeypatch):
        """只读诊断（ss -tlnp）→ 权限矩阵放行（None）→ 直接执行。"""
        _vault_cred(tmp_path, monkeypatch)
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission", lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host: {"type": "vault", "ref": "srv-pass"})

        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="Active: active", stderr="")

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        out = json.loads(_sudo_exec_handler(
            {"host": "localhost", "command": "ss -tlnp", "env": "dev"}))
        assert out["status"] == "ok"
        assert out["stdout"] == "Active: active"
        assert out["command"] == "sudo -A ss -tlnp"

    def test_missing_credential_fail_closed(self, monkeypatch):
        """凭据缺失 → 停下来问用户（fail-closed，不猜测不重试）。"""
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission", lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential", lambda host: None)
        out = _sudo_exec_handler({"host": "prod1", "command": "ss -tlnp", "env": "prod"})
        assert "缺少 sudo 凭据" in out
        assert "停止自动重试" in out


class TestHandlerSecurityBoundary:
    def test_redirect_rejected(self):
        out = _sudo_exec_handler(
            {"host": "h", "command": "ss -tlnp > /tmp/out", "env": "dev"})
        assert "拒绝 command" in out and "重定向" in out

    def test_bash_c_rejected(self):
        out = _sudo_exec_handler(
            {"host": "h", "command": "bash -c 'ss -tlnp'", "env": "dev"})
        assert "拒绝 command" in out and "嵌套 shell" in out

    def test_sudo_pipe_form_rejected(self):
        """工具自身不得拼出 §V 泄漏形态——命令里内嵌 sudo 直接拒绝。"""
        out = _sudo_exec_handler(
            {"host": "h", "command": "echo pw | sudo -S ss -tlnp", "env": "dev"})
        assert "拒绝 command" in out and "sudo" in out

    def test_command_substitution_rejected(self):
        out = _sudo_exec_handler(
            {"host": "h", "command": "cat $(find /tmp -name x)", "env": "dev"})
        assert "拒绝 command" in out and "命令替换" in out

    def test_background_rejected(self):
        out = _sudo_exec_handler(
            {"host": "h", "command": "ss -tlnp &", "env": "dev"})
        assert "拒绝 command" in out

    def test_empty_command_rejected(self):
        out = _sudo_exec_handler({"host": "h", "command": "", "env": "dev"})
        assert "需要 command" in out

    def test_readonly_pipeline_allowed(self, tmp_path, monkeypatch):
        """只读管道（ps aux | grep）是合法诊断形态，不拒绝。"""
        _vault_cred(tmp_path, monkeypatch)
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission", lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host: {"type": "vault", "ref": "srv-pass"})

        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="sshd", stderr="")

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        out = json.loads(_sudo_exec_handler(
            {"host": "localhost", "command": "ps aux | grep sshd", "env": "dev"}))
        assert out["status"] == "ok"
