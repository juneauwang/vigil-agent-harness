"""OPS-DELTA #7 — 退出汇总自动展示 session token 用量（纯 UI，零新数据逻辑）。

- 有 live agent 且 session_api_calls > 0 → 追加一行 token 汇总（数值与
  agent 属性一致）；
- 无 agent / 0 调用 → 不打印该行，现有退出摘要不受影响；
- ``_print_exit_summary(clear_screen=False)``（-q 模式）同样生效。
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _make_cli(agent=None):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.session_id = "20260524_000001_abc123"
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.agent = agent
    cli_obj._session_db = None
    cli_obj.session_start = datetime.now()
    return cli_obj


def _agent_with_tokens(input_tokens=1234, output_tokens=567, total=1801, calls=3):
    agent = MagicMock()
    agent.session_api_calls = calls
    agent.session_input_tokens = input_tokens
    agent.session_output_tokens = output_tokens
    agent.session_total_tokens = total
    return agent


class TestExitSummaryTokens:
    def test_token_line_printed_with_agent_values(self, capsys):
        agent = _agent_with_tokens()
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "📊 本次会话: 输入 1,234 · 输出 567 · 总计 1,801 tokens" in out
        # 现有退出摘要不受影响。
        assert "Messages:       1 (1 user, 0 tool calls)" in out

    def test_no_token_line_without_agent(self, capsys):
        cli_obj = _make_cli(agent=None)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "📊 本次会话" not in out
        assert "Resume this session with:" in out

    def test_no_token_line_with_zero_api_calls(self, capsys):
        agent = _agent_with_tokens(calls=0)
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "📊 本次会话" not in out
        assert "Tokens:" not in out

    def test_token_line_in_q_mode_clear_screen_false(self, capsys):
        """-q 模式（clear_screen=False）下退出汇总同样带 token 行。"""
        agent = _agent_with_tokens()
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "📊 本次会话: 输入 1,234 · 输出 567 · 总计 1,801 tokens" in out

    def test_token_line_skipped_when_agent_attrs_missing(self, capsys):
        """agent 无 token 属性（旧对象/损坏状态）→ 跳过而非崩溃。"""
        agent = MagicMock()
        del agent.session_api_calls
        del agent.session_input_tokens
        del agent.session_output_tokens
        del agent.session_total_tokens
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "Resume this session with:" in out
