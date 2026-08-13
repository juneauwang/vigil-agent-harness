"""批次十四 E4 — 会话 token 显示增强验收测试。

覆盖：token 行含缓存命中数 ``cache read A/B``（B=总输入，即命中率分母）；
已知模型（deepseek-v4-flash/pro）追加价格估算；未知模型省略价格不报错；
价格估算只读不改 token 统计。
"""

from __future__ import annotations

import pytest

import hermes_cli.main as main_mod
from hermes_cli.main import (
    _estimate_session_cost_usd,
    _format_session_token_line,
    _match_model_price,
)


def test_token_line_contains_cache_read_and_rate():
    line = _format_session_token_line(50000, 30000, 10000, 8000, 2000)
    assert "Tokens:" in line
    assert "in 30000" in line and "out 10000" in line
    # cache read A/B：A=命中缓存数，B=in+cache_read（命中率分母）。
    assert "cache read 8000/38000" in line
    assert "reasoning 2000" in line


def test_token_line_zero_tokens_no_crash():
    line = _format_session_token_line(0, 0, 0, 0, 0)
    assert "cache read 0/0" in line
    assert "(≈$" not in line


def test_token_line_known_model_has_price():
    line = _format_session_token_line(50000, 30000, 10000, 8000, 2000,
                                      model="deepseek/deepseek-v4-pro")
    assert "cache read 8000/38000" in line
    assert "(≈$0.03)" in line


def test_token_line_unknown_model_omits_price():
    line = _format_session_token_line(50000, 30000, 10000, 8000, 2000,
                                      model="gpt-5")
    assert "cache read 8000/38000" in line
    assert "(≈$" not in line


def test_token_line_model_none_omits_price():
    line = _format_session_token_line(100, 50, 30, 20, 0)
    assert "(≈$" not in line


def test_match_model_price_strips_provider_prefix():
    assert _match_model_price("deepseek-v4-pro") == "deepseek-v4-pro"
    assert _match_model_price("deepseek/deepseek-v4-pro") == "deepseek-v4-pro"
    assert _match_model_price("some/vendor/deepseek-v4-flash") == "deepseek-v4-flash"
    assert _match_model_price("gpt-5") is None
    assert _match_model_price(None) is None
    assert _match_model_price("") is None


def test_estimate_session_cost_usd_known():
    cost = _estimate_session_cost_usd("deepseek-v4-pro", 30000, 10000, 8000)
    # (30000*0.56 + 8000*0.056 + 10000*1.68) / 1e6
    assert cost == pytest.approx(0.034048)


def test_estimate_session_cost_usd_unknown_none():
    assert _estimate_session_cost_usd("gpt-5", 30000, 10000, 8000) is None
    assert _estimate_session_cost_usd(None, 1, 1, 1) is None


def test_price_table_keys_match_documented_models():
    assert set(main_mod._MODEL_PRICE_PER_MTOK) == {"deepseek-v4-flash", "deepseek-v4-pro"}
