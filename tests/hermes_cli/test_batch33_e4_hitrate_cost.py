"""批三十三 T3 — E4 补漏：退出汇总/状态栏 token 行命中率 + 估算成本。

覆盖：
- cli.HermesCLI._print_exit_summary：缓存命中率 X% + 估算成本（已知模型）；
  分母为 0 时不显示命中率；未知模型不显示成本；成本估算失败不影响汇总。
- hermes_cli.main._format_session_token_line（E4 状态栏 token 行）：命中率
  百分比随 A/B 输出；B=0 省略百分比；既有 cache read A/B 子串不破坏。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.main as main_mod
from cli import HermesCLI
from hermes_cli.main import _format_session_token_line


def _make_cli(agent=None):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.session_id = "20260816_000001_abc123"
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.agent = agent
    cli_obj._session_db = None
    cli_obj.session_start = datetime.now()
    cli_obj.model = ""
    cli_obj.provider = None
    return cli_obj


def _agent_with_usage(**overrides):
    agent = MagicMock()
    base = {
        "session_api_calls": 5,
        "session_input_tokens": 30000,
        "session_output_tokens": 10000,
        "session_total_tokens": 50000,
        "session_cache_read_tokens": 8000,
        "session_cache_write_tokens": 2000,
        "session_reasoning_tokens": 2000,
        "model": "deepseek-v4-flash",
        "provider": "deepseek",
        "base_url": None,
        "session_estimated_cost_usd": 0.0,
        "session_cost_status": "unknown",
    }
    for k, v in base.items():
        setattr(agent, k, overrides.get(k, v))
    return agent


class TestCliExitSummary:
    def test_exit_summary_has_hit_rate_and_cost(self, capsys):
        # deepseek-v4-flash 官方快照缺 cache-write 单价（usage_pricing 边界）：
        # cache_write=0 时估算可出。
        cli_obj = _make_cli(agent=_agent_with_usage(session_cache_write_tokens=0))
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        # 命中率 = 8000/(8000+30000) = 21.05% → 21.1%
        assert "缓存命中率 21.1%" in out
        # 已知模型 → 估算成本（usage_pricing 的 label，如 ~$0.0x）
        assert "估算成本" in out

    def test_exit_summary_no_hit_rate_when_zero_denominator(self, capsys):
        agent = _agent_with_usage(session_input_tokens=0, session_cache_read_tokens=0)
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "缓存命中率" not in out
        assert "📊 本次会话" in out

    def test_exit_summary_unknown_model_omits_cost(self, capsys):
        agent = _agent_with_usage(model="gpt-5")
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "估算成本" not in out
        assert "缓存命中率 21.1%" in out

    def test_exit_summary_cost_failure_does_not_crash(self, capsys):
        agent = _agent_with_usage()
        cli_obj = _make_cli(agent=agent)
        with patch("cli.estimate_usage_cost", side_effect=RuntimeError("boom")):
            with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
                cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "📊 本次会话" in out
        assert "估算成本" not in out


class TestE4TokenLineHitRate:
    def test_token_line_includes_hit_rate_percent(self):
        line = _format_session_token_line(50000, 30000, 10000, 8000, 2000)
        # A/B 子串保留（既有 batch14 断言），百分比追加。
        assert "cache read 8000/38000" in line
        assert "8000/38000 (21%)" in line

    def test_token_line_zero_total_omits_percent(self):
        line = _format_session_token_line(0, 0, 0, 0, 0)
        assert "cache read 0/0" in line
        assert "(0%)" not in line

    def test_token_line_known_model_price_unchanged(self):
        line = _format_session_token_line(50000, 30000, 10000, 8000, 2000,
                                          model="deepseek/deepseek-v4-pro")
        assert "cache read 8000/38000 (21%)" in line
        assert "(≈$0.03)" in line


class TestCostFallback:
    def test_deepseek_cache_write_unknown_falls_back_to_estimate(self, capsys):
        """deepseek 快照缺 cache-write 单价（estimate 返回 unknown）→ 降级为
        忽略 cache-write 的近似估算，真实会话也能看到价格。"""
        cli_obj = _make_cli(agent=_agent_with_usage(session_cache_write_tokens=2000))
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "估算成本" in out

    def test_accumulated_cost_preferred(self, capsys):
        """agent 会话内已累计成本（conversation_loop 路径）优先于现估。"""
        agent = _agent_with_usage()
        agent.session_estimated_cost_usd = 0.42
        agent.session_cost_status = "estimated"
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "估算成本 ≈$0.42" in out

    def test_accumulated_included_shows_subscription(self, capsys):
        agent = _agent_with_usage()
        agent.session_estimated_cost_usd = 0.0
        cli_obj = _make_cli(agent=agent)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch("cli.estimate_usage_cost") as mock_cost:
            # 订阅含价路由：amount_usd=0 + status=included（usage_pricing 语义）。
            mock_cost.return_value.amount_usd = 0.0
            mock_cost.return_value.status = "included"
            mock_cost.return_value.label = "included"
            cli_obj._print_exit_summary(clear_screen=False)
        out = capsys.readouterr().out
        assert "成本已含（订阅）" in out
