"""Tests for ``HermesCLI._prompt_text_input`` thread-safe input dispatch.

Raw ``input()`` prompts can race with prompt_toolkit when called from the TUI.
Since 批三十三 2a, ``_prompt_text_input`` runs through a prompt_toolkit-native
modal when a prompt_toolkit app is active on a non-main thread (process_loop /
slash-worker): the prompt is scheduled onto the app loop via
``call_soon_threadsafe`` and answered through a response queue — never a raw
``input()`` off the main thread. Without a schedulable app loop it cancels
cleanly to None (the old #23185 guard discipline).
"""

import threading
import time
from unittest.mock import MagicMock, patch


def _make_cli():
    """Minimal HermesCLI shell exposing prompt fallback helpers."""
    import cli as cli_mod

    obj = object.__new__(cli_mod.HermesCLI)
    obj._app = MagicMock()
    obj._status_bar_visible = True
    return obj


class _ImmediateLoop:
    """Synchronous stand-in for a prompt_toolkit event loop: callbacks run
    immediately on call_soon_threadsafe (as a real loop would, on its thread)."""

    def call_soon_threadsafe(self, fn):
        fn()


class TestPromptTextInputThreadSafety:
    def test_main_thread_uses_run_in_terminal(self):
        """On the main thread with an active app, route through run_in_terminal."""
        cli = _make_cli()

        with patch("prompt_toolkit.application.run_in_terminal") as mock_rit, \
             patch("builtins.input", return_value="2"):
            cli._prompt_text_input("Choice: ")

        # run_in_terminal was invoked; the _ask closure passed to it would
        # call input() when driven by the event loop.  We assert dispatch path,
        # not the orphaned-coroutine result.
        assert mock_rit.called

    def test_background_thread_routes_through_app_loop_modal(self):
        """On a daemon thread with an active app, prompt through the app-loop
        modal (批三十三 2a): no run_in_terminal, no raw input() — the typed
        answer arrives via the modal's response queue and is returned."""
        cli = _make_cli()
        cli._app.loop = _ImmediateLoop()

        result_holder = {}

        def run_on_daemon():
            with patch("prompt_toolkit.application.run_in_terminal") as mock_rit, \
                 patch("builtins.input", side_effect=AssertionError("input() must not be called off-main-thread")) as mock_input:
                result_holder["value"] = cli._prompt_text_input("Choice [1/2/3]: ", timeout=5)
                result_holder["rit_called"] = mock_rit.called
                result_holder["input_called"] = mock_input.called

        t = threading.Thread(target=run_on_daemon, daemon=True)
        t.start()

        # 等 modal 状态在主线程侧建立，模拟用户在输入区按 Enter 提交。
        deadline = time.monotonic() + 5
        while cli._text_input_state is None and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert cli._text_input_state is not None
        cli._text_input_state["response_queue"].put("node1")

        t.join(timeout=5)
        assert not t.is_alive(), "daemon thread hung — modal did not return"
        assert result_holder["value"] == "node1"
        assert result_holder["rit_called"] is False
        assert result_holder["input_called"] is False
        assert cli._text_input_state is None  # teardown 已还原

    def test_background_thread_empty_input_returns_none(self):
        """空输入（回车取消）→ None，与 input().strip() or None 语义一致。"""
        cli = _make_cli()
        cli._app.loop = _ImmediateLoop()

        result_holder = {}

        def run_on_daemon():
            result_holder["value"] = cli._prompt_text_input("Choice: ", timeout=5)

        t = threading.Thread(target=run_on_daemon, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while cli._text_input_state is None and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert cli._text_input_state is not None
        cli._text_input_state["response_queue"].put(None)
        t.join(timeout=5)
        assert not t.is_alive()
        assert result_holder["value"] is None

    def test_background_thread_schedule_failure_cancels(self):
        """App loop 无法调度（无真实事件循环，回调永不执行）→ 干净取消 None，
        不调 input()（旧 #23185 守卫纪律：不可安全 off-main prompt）。"""
        cli = _make_cli()  # MagicMock.loop 的 call_soon_threadsafe 不执行回调

        result_holder = {}

        def run_on_daemon():
            with patch("builtins.input", side_effect=AssertionError("input() must not be called")) as mock_input:
                result_holder["value"] = cli._prompt_text_input("Choice [1/2/3]: ", timeout=5)
                result_holder["input_called"] = mock_input.called

        t = threading.Thread(target=run_on_daemon, daemon=True)
        t.start()
        # ready.wait(5) 超时后取消返回。
        t.join(timeout=8)
        assert not t.is_alive(), "daemon thread hung — schedule failure did not cancel"
        assert result_holder["value"] is None
        assert result_holder["input_called"] is False

    def test_no_app_uses_direct_input(self):
        """Without an active prompt_toolkit app, always call input() directly."""
        cli = _make_cli()
        cli._app = None

        with patch("builtins.input", return_value="cancel") as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert mock_input.called
        assert result == "cancel"

    def test_run_in_terminal_exception_falls_back(self):
        """If run_in_terminal raises (WSL / Warp edge cases), fall back to input()."""
        cli = _make_cli()

        with patch(
            "prompt_toolkit.application.run_in_terminal",
            side_effect=RuntimeError("event loop dropped the coroutine"),
        ), patch("builtins.input", return_value="3") as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert mock_input.called
        assert result == "3"

    def test_eof_returns_none(self):
        """EOFError from input() yields None, not an unhandled exception."""
        cli = _make_cli()
        cli._app = None

        with patch("builtins.input", side_effect=EOFError()):
            result = cli._prompt_text_input("Choice: ")

        assert result is None
