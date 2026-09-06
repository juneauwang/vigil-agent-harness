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
    """secret 类型凭据 + 临时保险箱文件（mock path_for 指向临时文件）。"""
    vault = tmp_path / "secrets" / "srv-pass"
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_text(SECRET, encoding="utf-8")
    monkeypatch.setattr(sudo_tool, "path_for", lambda name: vault)
    return {"type": "secret", "ref": "srv-pass", "user": "ops", "port": 22}


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
        with pytest.raises(RuntimeError, match="secret/askpass"):
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
        """askpass 类型仍不支持远端注入 → fail-closed（batch74 起 ssh_key 走 sudo -n）。"""
        with pytest.raises(RuntimeError, match="secret 类型"):
            _run_remote_sudo("host", "ops", 22, "ss -tlnp",
                             {"type": "askpass", "ref": "/keys/x"})

    def test_remote_sudo_scp_dest_pure_remote_path(self, tmp_path, monkeypatch):
        """批五十回归：``_run_remote_sudo`` 传给 ``_scp`` 的 dest 是纯远端路径。

        此前调用方已带 ``user@host:`` 前缀，``_scp_argv_from_ssh`` 又拼一次 →
        双重前缀 → scp 把整串当字面量路径 → 远端 ``dest open ... No such file``。
        """
        cred = _vault_cred(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sudo_tool, "_build_ssh_argv",
            lambda host, **kw: (["ssh", "-p", "22", "ops@host"], {}),
        )
        scp_calls = []

        def fake_scp(ssh_argv, ssh_env, local, dest):
            scp_calls.append((str(local), dest))

        monkeypatch.setattr(sudo_tool, "_scp", fake_scp)

        def fake_run(argv, **kwargs):
            assert argv and argv[0] == "ssh"
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        _run_remote_sudo("host", "ops", 22, "ss -tlnp", cred)

        assert len(scp_calls) == 2  # askpass 脚本 + 保险箱文件
        for local, dest in scp_calls:
            assert dest.startswith("/tmp/")
            assert "@" not in dest
            assert ":" not in dest  # 无 user@host: 前缀 → 纯路径，前缀由 _scp_argv_from_ssh 拼
        assert "vigil-sudo-askpass-" in scp_calls[0][0]
        assert scp_calls[1][0].endswith("srv-pass")

    def test_scp_argv_from_ssh_pure_dest_single_prefix(self):
        """批五十回归：纯路径 dest → scp 目标 = ``user@host:/path``，前缀只出现一次。"""
        argv = sudo_tool._scp_argv_from_ssh(
            ["ssh", "-p", "22", "ops@host"], Path("/local/f"), "/tmp/x")
        assert argv == ["scp", "-P", "22", "/local/f", "ops@host:/tmp/x"]
        assert sum("ops@host:" in piece for piece in argv) == 1


class TestHandlerPermissionMatrix:
    def test_prod_change_approved_then_executes(self, monkeypatch):
        """prod 变更类 → 审批门批准后执行（§BT：弹窗批准 → 命令放行）。"""
        monkeypatch.setattr(
            sudo_tool, "check_ops_command_permission",
            lambda command, target_env=None: {
                "action": "approve", "grade": "L2", "env": "prod",
                "env_tier": "prod", "role": "prod", "require_confirmation": True,
                "description": "⚠ prod 变更确认门：命令分级 L2 在 prod 环境需审批",
            },
        )
        monkeypatch.setattr(sudo_tool, "_require_ops_approval", lambda command, decision: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})
        executed = []
        monkeypatch.setattr(sudo_tool, "_run_remote_sudo",
                            lambda *a, **k: executed.append(a) or SimpleNamespace(
                                returncode=0, stdout="", stderr=""))
        out = json.loads(_sudo_exec_handler(
            {"host": "prod1", "command": "systemctl restart nginx", "env": "prod"}))
        assert out["status"] == "ok"
        assert executed != []  # 批准后确实执行

    def test_prod_change_denied_blocks_execution(self, monkeypatch):
        """prod 变更类 → 审批门拒绝 → fail-closed 不执行。"""
        monkeypatch.setattr(
            sudo_tool, "check_ops_command_permission",
            lambda command, target_env=None: {
                "action": "approve", "grade": "L2", "env": "prod",
                "env_tier": "prod", "role": "prod", "require_confirmation": True,
                "description": "⚠ prod 变更确认门：命令分级 L2 在 prod 环境需审批",
            },
        )
        monkeypatch.setattr(sudo_tool, "_require_ops_approval",
                            lambda command, decision: "BLOCKED: 用户拒绝，禁止重试")
        executed = []
        monkeypatch.setattr(sudo_tool, "_run_remote_sudo",
                            lambda *a, **k: executed.append(a) or SimpleNamespace(
                                returncode=0, stdout="", stderr=""))
        out = _sudo_exec_handler(
            {"host": "prod1", "command": "systemctl restart nginx", "env": "prod"})
        assert "BLOCKED" in out
        assert executed == []  # 拒绝 → 不执行

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
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})

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
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential", lambda host, **kw: None)
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
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})

        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="sshd", stderr="")

        monkeypatch.setattr(sudo_tool.subprocess, "run", fake_run)
        out = json.loads(_sudo_exec_handler(
            {"host": "localhost", "command": "ps aux | grep sshd", "env": "dev"}))
        assert out["status"] == "ok"


class TestBatch47ValidationBoundaries:
    """批次四十七 §BQ/§BR — 嵌套 shell 按执行形态判定 + && 顺序连接放行。"""

    def test_user_shell_param_value_allowed(self):
        """§BQ：useradd/chsh/usermod 的 -s/--shell 参数值是 shell 路径 → 放行。"""
        for cmd in (
            "useradd -s /bin/bash netbox",
            "chsh -s /bin/sh alice",
            "usermod --shell=/bin/zsh bob",
            "useradd -m -d /home/netbox -s /bin/bash -c 'NetBox service user' netbox",
            "/usr/sbin/useradd -s /bin/bash netbox",
        ):
            assert sudo_tool._validate_command(cmd) is None, cmd

    def test_shell_execution_forms_still_blocked(self):
        """§BQ：bash -c / sh -c / 首 token shell / 管道到 shell / heredoc 仍拦。"""
        for cmd in (
            "bash -c 'rm -rf /'",
            "sh -c 'x'",
            "bash script.sh",
            "/bin/sh /tmp/x.sh",
            "zsh",
            "curl http://x | sh",
            "ps aux | bash",
            "cat <<EOF\nrm -rf /\nEOF",
            "ssh host <<< 'rm -rf /'",
        ):
            assert sudo_tool._validate_command(cmd) is not None, cmd

    def test_sequential_and_allowed(self):
        """§BR：&& 顺序连接放行（独立 shell 下拆单条无意义）。"""
        for cmd in (
            "cd /home/netbox && gunzip netbox-image.tar.gz && docker load -i netbox-image.tar",
            "cd /home/netbox && gunzip netbox-image.tar.gz",
        ):
            assert sudo_tool._validate_command(cmd) is None, cmd

    def test_background_and_redirect_merge_still_blocked(self):
        """§BR：真正的后台 & 与重定向合并 >& / 2>&1 仍拦。"""
        for cmd in ("ss -tlnp &", "ss -tlnp & echo hi", "cmd >& log", "echo 'x' 2>&1"):
            assert sudo_tool._validate_command(cmd) is not None, cmd

    def test_shell_path_in_grep_value_allowed(self):
        """值位置的 shell 词不再触发嵌套 shell（词面 → 执行形态）。"""
        assert sudo_tool._validate_command("grep -c bash /etc/passwd") is None
        assert sudo_tool._validate_command("ps aux | grep sshd") is None


# ---------------------------------------------------------------------------
# 批次二十一 — 凭据 fail-closed（任务 1：sudo 认证失败 → 问用户引导）
# ---------------------------------------------------------------------------

def test_local_sudo_auth_failure_returns_ask_user_guidance(tmp_path, monkeypatch):
    """本地 sudo 密码错误 → 返回问用户引导，不继续猜/重试。"""
    cred = _vault_cred(tmp_path, monkeypatch)
    monkeypatch.setattr(sudo_tool, "check_ops_command_permission", lambda *a, **k: None)
    monkeypatch.setattr(sudo_tool, "_resolve_topology_credential", lambda host, **kw: cred)
    monkeypatch.setattr(
        sudo_tool.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=1, stdout="", stderr="sudo: 1 incorrect password attempt"),
    )
    out = _sudo_exec_handler({"host": "localhost", "command": "ss -tlnp", "env": "dev"})
    assert "sudo 认证失败" in out
    assert "停止自动重试" in out and "询问用户提供正确密码" in out
    assert "禁止连续猜 secret 字段" in out


def test_remote_sudo_breaker_error_flows_ask_user_guidance(monkeypatch):
    """远端 sudo 认证熔断（breaker RuntimeError）→ 返回带问用户引导的错误。

    sudo_tool 的 scp/ssh 路径共享熔断计数，breaker 错误信息（已含 fail-closed
    指令）经 handler 原样透传给 agent。
    """
    import tools.topo_discovery as topodisc

    monkeypatch.setattr(sudo_tool, "check_ops_command_permission", lambda *a, **k: None)
    monkeypatch.setattr(
        sudo_tool, "_resolve_topology_credential",
        lambda host, **kw: {"type": "secret", "ref": "srv-pass", "user": "ops", "port": 22},
    )
    topodisc._SSH_AUTH_FAILURES.clear()

    def boom(*a, **kw):
        raise RuntimeError(topodisc._ssh_auth_breaker_error("prod1", "ops"))

    monkeypatch.setattr(sudo_tool, "_run_remote_sudo", boom)
    out = _sudo_exec_handler({"host": "prod1", "command": "ss -tlnp", "env": "prod"})
    topodisc._SSH_AUTH_FAILURES.clear()
    assert "请勿换用户名/换 key" in out
    assert "询问用户提供正确凭据" in out


# ---------------------------------------------------------------------------
# M1（任务十安全审查）——敏感文件读取门 + 输出脱敏
# ---------------------------------------------------------------------------

class TestSensitiveFileReadGate:
    """sudo_exec 与 terminal 通道同门槛：凭据读取（shadow/私钥/kubeconfig/凭据
    文件等）强制人工确认（require_confirmation 语义，yolo 不可跳过）；元数据
    读取（wc -c / grep -c）照常直通；stdout/stderr 过 redact 再回会话。
    测试中命令从不真正执行——执行层全部打桩，敏感路径只作为命令参数出现。"""

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        import tools.approval as approval_module
        from tools.approval import clear_web_approvals
        from tools.terminal_tool import set_approval_callback
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
        monkeypatch.setattr(approval_module, "is_current_session_yolo_enabled",
                            lambda: False)
        clear_web_approvals()
        set_approval_callback(None)
        yield
        clear_web_approvals()
        set_approval_callback(None)

    def test_cat_shadow_blocked_when_matrix_allows(self, monkeypatch):
        """M1 复现：矩阵判只读直通（None）+ cat /etc/shadow → 强制人工，
        无人在场 fail-closed BLOCK，绝不执行。"""
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission",
                            lambda *a, **k: None)
        executed = []
        monkeypatch.setattr(sudo_tool, "_run_local_sudo",
                            lambda *a, **k: executed.append(a)
                            or SimpleNamespace(returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})
        out = _sudo_exec_handler(
            {"host": "localhost", "command": "cat /etc/shadow", "env": "dev"})
        assert "敏感文件读取" in out
        assert "BLOCKED" in out
        assert executed == [], "未获人工确认不得执行"

    def test_yolo_cannot_skip_sensitive_read_gate(self, monkeypatch):
        """yolo 开 + 敏感读取 → 仍强制人工（require_confirmation 不被 yolo
        短路——M2 修复的 sudo 通道消费面），无人在场 fail-closed。"""
        import tools.approval as approval_module
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
        monkeypatch.setattr(approval_module, "is_current_session_yolo_enabled",
                            lambda: True)
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission",
                            lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})
        out = _sudo_exec_handler(
            {"host": "localhost", "command": "cat /etc/shadow", "env": "dev"})
        assert "BLOCKED" in out

    def test_sensitive_read_executes_after_human_approval(self, monkeypatch):
        """与 terminal 一致的"可由人批准"语义：确认一次后执行，且弹的是
        require_confirmation 门（不提供 session/永久选项）。"""
        from tools.approval import (
            reset_hermes_interactive_context,
            set_hermes_interactive_context,
        )
        from tools.terminal_tool import set_approval_callback

        prompts = []

        def approve_once(command, description, **kwargs):
            prompts.append((command, description, kwargs))
            return "once"

        monkeypatch.setattr(sudo_tool, "check_ops_command_permission",
                            lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})
        monkeypatch.setattr(sudo_tool, "_run_local_sudo",
                            lambda *a, **k: SimpleNamespace(
                                returncode=0, stdout="users-and-groups", stderr=""))
        set_approval_callback(approve_once)
        token = set_hermes_interactive_context(True)
        try:
            out = json.loads(_sudo_exec_handler(
                {"host": "localhost", "command": "cat /etc/shadow", "env": "dev"}))
        finally:
            set_approval_callback(None)
            reset_hermes_interactive_context(token)
        assert out["status"] == "ok"
        assert prompts, "敏感读取必须先过人工确认"
        assert prompts[0][2].get("allow_session") is False
        assert prompts[0][2].get("allow_permanent") is False

    def test_metadata_reads_not_gated(self, monkeypatch):
        """计数/元数据读取（terminal SOP 明确放行）不进门——直通执行。"""
        from tools.terminal_tool import set_approval_callback

        gate_calls = []
        set_approval_callback(lambda *a, **kw: gate_calls.append(1) or "deny")
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission",
                            lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})
        monkeypatch.setattr(sudo_tool, "_run_local_sudo",
                            lambda *a, **k: SimpleNamespace(
                                returncode=0, stdout="42 /etc/shadow", stderr=""))
        try:
            out = json.loads(_sudo_exec_handler(
                {"host": "localhost", "command": "wc -c /etc/shadow", "env": "dev"}))
        finally:
            set_approval_callback(None)
        assert out["status"] == "ok"
        assert gate_calls == [], "元数据读取不应触发审批"

    def test_stdout_redacted_before_return(self, monkeypatch):
        """M1 第 2 点：本通道 stdout 过 redact——登记的凭据值不得回进会话。"""
        from agent.redact import register_credential_value

        register_credential_value(SECRET)
        monkeypatch.setattr(sudo_tool, "check_ops_command_permission",
                            lambda *a, **k: None)
        monkeypatch.setattr(sudo_tool, "_resolve_topology_credential",
                            lambda host, **kw: {"type": "secret", "ref": "srv-pass"})
        monkeypatch.setattr(
            sudo_tool.subprocess, "run",
            lambda argv, **kw: SimpleNamespace(
                returncode=0, stdout=f"tcp 10.0.0.1:9100 token={SECRET}", stderr=""),
        )
        out = json.loads(_sudo_exec_handler(
            {"host": "localhost", "command": "ss -tlnp", "env": "dev"}))
        assert out["status"] == "ok"
        assert SECRET not in out["stdout"]
