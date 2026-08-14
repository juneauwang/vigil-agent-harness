"""批次二十 任务 2/3 — 审批/clarify 超时策略 wait 化。

默认 approvals.timeout_policy="wait"：超时不再静默 deny——fail-closed 保持
pending（绝不自动批准），超时只作"提醒间隔"，多 session 用户仍可在任一会话
回答；"deny" 保留旧行为（超时 = 未响应拒绝）。clarify 同策略对齐（wait 超时
后不让 LLM 自猜，继续等待用户回答）。

钉住：
  - config_defaults 默认值 = wait；
  - gateway 等待循环：wait 超时后重推通知并保持 pending，deny 一次通知后
    timeout 返回；
  - CLI 交互框（cli.py _approval_callback）：wait 超时继续等待，deny 超时
    返回 "timeout"；
  - 非交互 input() 循环：wait 超时后继续等用户输入（不返回 timeout）；
  - clarify 路径：wait 超时后不自动让 LLM 决定。
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cli as cli_module
from cli import HermesCLI


# ---------------------------------------------------------------------------
# Task 2 — approvals.timeout_policy 默认值与 gateway 等待循环
# ---------------------------------------------------------------------------

def test_config_default_timeout_policy_is_wait():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["approvals"]["timeout_policy"] == "wait"


def _approval_data():
    return {
        "command": "rm -rf /tmp/whatever",
        "description": "recursive delete",
        "pattern_key": "rm_rf",
        "pattern_keys": ["rm_rf"],
    }


class TestGatewayWaitPolicy:
    SESSION = "wait-policy-session"

    def setup_method(self):
        from tools import approval as mod

        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod._session_approved.clear()
        mod._permanent_approved.clear()
        mod._pending.clear()
        mod.set_current_session_key(self.SESSION)

    def teardown_method(self):
        from tools import approval as mod

        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod.clear_session(self.SESSION)

    def test_wait_policy_renotifies_and_stays_pending(self, monkeypatch):
        from tools import approval as mod

        monkeypatch.setattr(mod, "_get_approval_timeout", lambda: 0.05)
        monkeypatch.setattr(mod, "_get_approval_timeout_policy", lambda: "wait")

        notified = []

        def notify(data):
            notified.append(dict(data))
            # Second notification = the wait-mode re-push fired after the
            # first interval expired. Resolve it like a late /deny.
            if len(notified) >= 2:
                mod.resolve_gateway_approval(self.SESSION, "deny")

        result = mod._await_gateway_decision(self.SESSION, notify, _approval_data())

        # The user was re-notified after the first interval — a multi-session
        # user who missed the first push still got a chance to decide.
        assert len(notified) >= 2, "wait policy should re-notify after timeout"
        assert result == {"resolved": True, "choice": "deny", "reason": None}

    def test_deny_policy_times_out_after_single_notify(self, monkeypatch):
        from tools import approval as mod

        monkeypatch.setattr(mod, "_get_approval_timeout", lambda: 0.05)
        monkeypatch.setattr(mod, "_get_approval_timeout_policy", lambda: "deny")

        notified = []

        def notify(data):
            notified.append(dict(data))

        result = mod._await_gateway_decision(self.SESSION, notify, _approval_data())

        # Legacy contract: deadline is a hard cutoff, no re-notify.
        assert result == {"resolved": False, "choice": None, "reason": None}
        assert len(notified) == 1


# ---------------------------------------------------------------------------
# Task 2 — CLI 交互框（cli.py _approval_callback，实际注册的 callback）
# ---------------------------------------------------------------------------

def _make_cli_stub():
    cli = HermesCLI.__new__(HermesCLI)
    cli._approval_state = None
    cli._approval_deadline = 0
    cli._approval_lock = threading.Lock()
    cli._sudo_state = None
    cli._sudo_deadline = 0
    cli._modal_input_snapshot = None
    cli._invalidate = MagicMock()
    cli._app = SimpleNamespace(invalidate=MagicMock(), current_buffer=SimpleNamespace(
        text="", reset=lambda *a, **k: None))
    return cli


class TestCliCallbackTimeoutPolicy:
    def test_wait_policy_keeps_waiting_past_deadline(self):
        cli = _make_cli_stub()
        cfg = {"approvals": {"timeout": 1, "timeout_policy": "wait"}}
        result = {}

        def _run():
            result["value"] = cli._approval_callback("rm -rf /tmp/x", "desc")

        with patch("cli.CLI_CONFIG", cfg), patch.object(cli_module, "_cprint"):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            deadline = time.time() + 3
            while cli._approval_state is None and time.time() < deadline:
                time.sleep(0.01)
            assert cli._approval_state is not None

            # Wait comfortably past the 1s deadline — the prompt must NOT
            # have silently denied itself.
            time.sleep(1.5)
            assert thread.is_alive(), "wait policy must keep the prompt pending"
            assert cli._approval_state is not None

            cli._approval_state["response_queue"].put("once")
            thread.join(timeout=3)
        assert result["value"] == "once"

    def test_deny_policy_returns_timeout_at_deadline(self):
        cli = _make_cli_stub()
        cfg = {"approvals": {"timeout": 1, "timeout_policy": "deny"}}
        result = {}

        def _run():
            result["value"] = cli._approval_callback("rm -rf /tmp/x", "desc")

        with patch("cli.CLI_CONFIG", cfg), patch.object(cli_module, "_cprint"):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=5)
        assert result.get("value") == "timeout"


# ---------------------------------------------------------------------------
# Task 2 — 非交互 input() 循环（prompt_dangerous_approval）
# ---------------------------------------------------------------------------

def test_input_loop_wait_policy_keeps_waiting(monkeypatch, capsys):
    """wait 模式：join 超时不是截止时间——继续等同一个 input()，用户晚到也能答。"""
    import builtins

    from tools.approval import prompt_dangerous_approval

    def slow_input(_prompt=""):
        time.sleep(0.3)
        return "once"

    monkeypatch.setattr(builtins, "input", slow_input)
    monkeypatch.setattr("tools.approval._get_approval_timeout_policy", lambda: "wait")

    result = prompt_dangerous_approval(
        "rm -rf /tmp/x", "recursive delete", timeout_seconds=0.05,
    )

    assert result == "once"
    rendered = capsys.readouterr().out
    assert "审批仍在等待" in rendered


# ---------------------------------------------------------------------------
# Task 3 — clarify 超时对齐（wait 不自动让 LLM 决定）
# ---------------------------------------------------------------------------

class TestClarifyTimeoutPolicy:
    def _clarify_stub(self):
        cli = SimpleNamespace(
            _clarify_state=None, _clarify_deadline=None, _clarify_freetext=False,
            _app=SimpleNamespace(invalidate=MagicMock()),
        )
        return cli

    def test_wait_policy_keeps_waiting_past_deadline(self):
        from hermes_cli.callbacks import clarify_callback
        cli = self._clarify_stub()
        cfg = {"agent": {"clarify_timeout": 1}, "approvals": {"timeout_policy": "wait"}}
        result = {}
        def _run():
            result["value"] = clarify_callback(cli, "Pick one", ["A", "B"])
        with patch("cli.CLI_CONFIG", cfg):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            deadline = time.time() + 3
            while cli._clarify_state is None and time.time() < deadline:
                time.sleep(0.01)
            assert cli._clarify_state is not None
            time.sleep(1.5)  # 越过 clarify deadline
            assert thread.is_alive(), "wait policy must keep clarify pending"
            cli._clarify_state["response_queue"].put("A")
            thread.join(timeout=3)
        assert result["value"] == "A"

    def test_deny_policy_auto_skips_at_deadline(self):
        from hermes_cli.callbacks import clarify_callback
        cli = self._clarify_stub()
        cfg = {"agent": {"clarify_timeout": 1}, "approvals": {"timeout_policy": "deny"}}
        result = {}
        def _run():
            result["value"] = clarify_callback(cli, "Pick one", ["A", "B"])
        with patch("cli.CLI_CONFIG", cfg), patch("hermes_cli.callbacks.cprint"):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(timeout=5)
        assert "best judgement" in result.get("value", "")
