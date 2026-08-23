"""批次二十 任务 1 — 审批框选项按场景过滤（session/always 无效选项）。

prod 变更确认门（require_confirmation）会跳过 session/permanent allowlist，审批
框仍显示 "session"/"always" 会让用户以为"我允许了"但下次照样弹。修法：场景标志
（allow_permanent/allow_session）贯穿所有 surface，交互框按标志过滤选项。

钉住：
  - callbacks.py / cli.py 交互框 choices 按 allow_permanent/allow_session 过滤；
  - check_all_command_guards 对 prod 确认门传 allow_permanent=False +
    allow_session=False 给回调（→ 只剩 once/deny）；
  - 非 prod 普通审批仍保留两个作用域（四选项场景不回归）；
  - 旧签名回调（无 allow_session kwarg）不因新 kwarg 崩溃（兼容旧调用）。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cli as cli_module
from cli import HermesCLI
from hermes_cli.callbacks import approval_callback as legacy_cli_callback


def _stub_cli():
    cli = SimpleNamespace(
        _approval_lock=threading.Lock(),
        _approval_state=None,
        _approval_deadline=0,
        _app=SimpleNamespace(invalidate=MagicMock()),
    )
    return cli


def _drive_callback(cli, *, cmd="rm -rf /tmp/x", **kwargs):
    """Run an approval callback on a background thread and wait for the modal
    state to appear. Returns (thread, result_holder)."""
    result = {}

    def _run():
        result["value"] = legacy_cli_callback(
            cli, cmd, "recursive delete", **kwargs
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.time() + 3
    while cli._approval_state is None and time.time() < deadline:
        time.sleep(0.01)
    assert cli._approval_state is not None, "approval panel never appeared"
    return t, result


class TestCallbackChoicesFilter:
    """hermes_cli/callbacks.py approval_callback choices follow the flags."""

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({}, ["once", "session", "always", "deny"]),
            ({"allow_permanent": False}, ["once", "session", "deny"]),
            # session 无效但 permanent 有效（理论组合）：只去 session。
            ({"allow_session": False}, ["once", "always", "deny"]),
            ({"allow_permanent": False, "allow_session": False}, ["once", "deny"]),
        ],
    )
    def test_choices_filtered_by_scenario(self, kwargs, expected):
        cli = _stub_cli()
        with patch("cli.CLI_CONFIG", {"approvals": {"timeout": 300, "timeout_policy": "deny"}}):
            t, result = _drive_callback(cli, **kwargs)
            assert cli._approval_state["choices"] == expected
            cli._approval_state["response_queue"].put("deny")
            t.join(timeout=3)
        assert result["value"] == "deny"

    def test_long_command_appends_view(self):
        cli = _stub_cli()
        long_cmd = "rm -rf " + "a" * 200
        with patch("cli.CLI_CONFIG", {"approvals": {"timeout": 300, "timeout_policy": "deny"}}):
            t, result = _drive_callback(cli, cmd=long_cmd,
                                        allow_permanent=False,
                                        allow_session=False)
            assert cli._approval_state["choices"] == ["once", "deny", "view"]
            cli._approval_state["response_queue"].put("deny")
            t.join(timeout=3)
        assert result["value"] == "deny"


class TestCliApprovalChoices:
    """cli.py HermesCLI._approval_choices — the live CLI registration point."""

    def test_default_four_choices(self):
        cli = HermesCLI.__new__(HermesCLI)
        assert cli._approval_choices("rm -rf /tmp/x") == [
            "once", "session", "always", "deny",
        ]

    def test_no_always_when_permanent_unavailable(self):
        cli = HermesCLI.__new__(HermesCLI)
        assert cli._approval_choices("rm -rf /tmp/x", allow_permanent=False) == [
            "once", "session", "deny",
        ]

    def test_prod_gate_hides_session_and_always(self):
        cli = HermesCLI.__new__(HermesCLI)
        assert cli._approval_choices(
            "rm -rf /tmp/x",
            allow_permanent=False,
            allow_session=False,
        ) == ["once", "deny"]

    def test_smart_denied_overrides_to_once_deny(self):
        cli = HermesCLI.__new__(HermesCLI)
        assert cli._approval_choices(
            "rm -rf /tmp/x", allow_permanent=True, smart_denied=True,
        ) == ["once", "deny"]

    def test_view_appended_last(self):
        cli = HermesCLI.__new__(HermesCLI)
        long_cmd = "rm -rf " + "a" * 200
        assert cli._approval_choices(long_cmd, allow_permanent=False,
                                     allow_session=False) == ["once", "deny", "view"]


class TestProdGateScenarioFlags:
    """check_all_command_guards 对 prod 确认门传 False/False 给回调。"""

    SESSION = "choices-filter-prod"

    @pytest.fixture
    def ops_env(self, tmp_path, monkeypatch):
        import hermes_cli.config as hc
        from tools import approval as mod

        token = mod.set_current_session_key(self.SESSION)
        (tmp_path / "config.yaml").write_text(
            "approvals:\n"
            "  mode: manual\n"
            "ops:\n"
            "  permissions:\n"
            "    enabled: true\n"
            "    env: prod\n"
            "    role: prod\n",
            encoding="utf-8",
        )
        # P5 操作矩阵（OPS-DELTA #75）把 L1-L4 分级换成 action × env 矩阵：
        # 确认门场景必须由矩阵显式配出 {approve: required}，空矩阵下 restart ×
        # prod 只是普通 approve（session/permanent 保持开启）。
        (tmp_path / "matrix.yaml").write_text(
            "schema_version: 1\n"
            "matrix:\n"
            "  prod:\n"
            "    restart: {approve: required}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
        hc._LOAD_CONFIG_CACHE.clear()
        mod._YOLO_MODE_FROZEN = False
        mod.clear_session(self.SESSION)
        mod._permanent_approved.clear()
        yield
        hc._LOAD_CONFIG_CACHE.clear()
        mod.clear_session(self.SESSION)
        mod.reset_current_session_key(token)

    def test_prod_confirmation_gate_passes_false_false(self, ops_env, monkeypatch):
        from tools import approval as mod

        captured = {}

        def approval_callback(command, description, **kwargs):
            captured.update(kwargs)
            return "deny"

        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        result = mod.check_all_command_guards(
            "systemctl restart myapp", "local",
            approval_callback=approval_callback,
        )

        assert result["approved"] is False
        # 确认门确实触发（描述带矩阵强制人工确认文案），且回调收到了 False/False。
        assert "强制人工确认" in result.get("description", "")
        # 确认门场景：session/always 都无效 → 两个作用域都必须关闭。
        assert captured.get("allow_permanent") is False
        assert captured.get("allow_session") is False

    def test_normal_approval_keeps_both_scopes(self, ops_env, monkeypatch):
        """非确认门普通审批（git push / pip install 在 prod 为 approve 非确认）→
        两个作用域保持开启（四选项场景不回归）。"""
        from tools import approval as mod

        captured = {}

        def approval_callback(command, description, **kwargs):
            captured.update(kwargs)
            return "deny"

        monkeypatch.setenv("VIGIL_INTERACTIVE", "1")
        result = mod.check_all_command_guards(
            "git push", "local",
            approval_callback=approval_callback,
        )

        assert result["approved"] is False
        assert captured.get("allow_permanent") is True
        assert captured.get("allow_session") is True


class TestLegacyCallbackCompat:
    """旧签名回调（未加 allow_session kwarg）不能因新 kwarg 崩溃。"""

    def test_callback_without_allow_session_kwarg(self):
        from tools.approval import prompt_dangerous_approval

        seen = {}

        def cb(command, description, *, allow_permanent=True):
            seen["command"] = command
            seen["allow_permanent"] = allow_permanent
            return "deny"

        result = prompt_dangerous_approval(
            "rm -rf /tmp/x", "recursive delete", approval_callback=cb
        )
        assert result == "deny"
        assert seen["command"] == "rm -rf /tmp/x"
        assert seen["allow_permanent"] is True

    def test_kwargs_callback_receives_allow_session(self):
        from tools.approval import prompt_dangerous_approval

        seen = {}

        def cb(command, description, **kwargs):
            seen.update(kwargs)
            return "deny"

        result = prompt_dangerous_approval(
            "rm -rf /tmp/x", "recursive delete",
            allow_permanent=False,
            allow_session=False,
            approval_callback=cb,
        )
        assert result == "deny"
        assert seen.get("allow_permanent") is False
        assert seen.get("allow_session") is False
