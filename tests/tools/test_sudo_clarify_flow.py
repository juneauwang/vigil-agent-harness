"""OPS-DELTA 批次三十二 — sudo_exec 无凭据时的内置 clarify 引导流程。

拓扑表无该 host credential 且用户要 sudo 时，工具内部走 clarify 收密码 →
credential_vault.store()（0600）→ 执行 → 拓扑登记提示；无交互通道时保持
原 fail-closed 引导（不设计裸 askpass/明文文件）。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import redact
from tools import sudo_tool
from tools.sudo_tool import _sudo_exec_handler


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    # batch83-fix（OPS-DELTA #99）：矩阵缺失 → deny（fail-closed）。本套件测
    # sudo clarify 引导流，与权限矩阵无关——写显式 ops.permissions.enabled=false
    # 的最小 config，避免 sudo 路径被"矩阵未初始化"提前拦截（sudo 经
    # check_ops_command_permission 裁决，显式关闭后交回既有检查）。
    (tmp_path / "config.yaml").write_text(
        "ops:\n  permissions:\n    enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    import hermes_cli.config as hc
    hc._LOAD_CONFIG_CACHE.clear()
    redact._reset_registered_credential_values_for_tests()
    sudo_tool.set_clarify_callback(None)
    sudo_tool._resolve_topology_credential = lambda host, allow_fallback=False: None
    yield
    sudo_tool.set_clarify_callback(None)
    redact._reset_registered_credential_values_for_tests()


def _fake_run(result, calls: dict):
    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        askpass = Path(kwargs["env"]["SUDO_ASKPASS"])
        calls["askpass_content"] = askpass.read_text(encoding="utf-8")
        return result
    return fake_run


class TestClarifyCollectFlow:
    def test_clarify_store_execute_local(self, tmp_path, monkeypatch):
        """无凭据 → clarify 收密码 → vault store(0600) → 本地 sudo -A 执行。"""
        prompt_log = []
        def my_clarify(question, choices=None, multi_select=False):
            prompt_log.append(question)
            return "wwplove815"
        sudo_tool.set_clarify_callback(my_clarify)

        calls = {}
        monkeypatch.setattr(
            sudo_tool.subprocess, "run",
            _fake_run(SimpleNamespace(returncode=0, stdout="ok", stderr=""), calls),
        )

        result = json.loads(_sudo_exec_handler(
            {"host": "localhost", "command": "ss -tlnp"}
        ))
        assert result["status"] == "ok"
        # 拓扑登记提示
        assert "_warning" in result and "sudo-localhost" in result["_warning"]
        assert "credential 声明" in result["_warning"]
        # vault 文件 600 + 值
        vault = Path(tmp_path) / "secrets" / "sudo-localhost"
        assert vault.is_file()
        assert stat.S_IMODE(vault.stat().st_mode) == 0o600
        assert vault.read_text(encoding="utf-8") == "wwplove815"
        # 值已登记 → 全局打码
        assert "wwplove815" in redact.registered_credential_values()
        # askpass 只 cat 保险箱文件，不含明文
        assert "wwplove815" not in calls.get("askpass_content", "")
        assert "cat" in calls.get("askpass_content", "")
        # 提问文本含敏感关键词（触发答复登记）
        assert "sudo 密码" in prompt_log[0]

    def test_auth_failure_hints_stored_credential(self, tmp_path, monkeypatch):
        sudo_tool.set_clarify_callback(lambda q, c=None, m=False: "wrong-pw-123")
        calls = {}
        monkeypatch.setattr(
            sudo_tool.subprocess, "run",
            _fake_run(
                SimpleNamespace(returncode=1, stdout="", stderr="incorrect password"),
                calls,
            ),
        )
        raw = _sudo_exec_handler(
            {"host": "localhost", "command": "ss -tlnp"}
        )
        assert raw.startswith('{"error"')
        text = raw
        assert "认证失败" in text
        assert "sudo-localhost" in text  # 提示已存凭据名

    def test_no_callback_fail_closed_guidance(self):
        sudo_tool.set_clarify_callback(None)
        result = _sudo_exec_handler({"host": "localhost", "command": "ss -tlnp"})
        assert "缺少 sudo 凭据" in result
        assert "禁止翻 ~/.ssh/" in result

    def test_vault_name_sanitized(self):
        assert sudo_tool._vault_name_for_host("10.0.0.5") == "sudo-10.0.0.5"
        assert sudo_tool._vault_name_for_host("my host!") == "sudo-my-host"
        assert sudo_tool._vault_name_for_host("") == "sudo-local"
