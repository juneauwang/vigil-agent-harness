"""批六十四：LLM 价格三级来源（tools/pricing.py，OPS-DELTA #79）。

覆盖：三级优先级（manual > online > builtin）、currency 校验、费用计算、
内置兜底（usage_pricing 官方快照 → 估算表）、在线拉取（成功写缓存 / 失败
不阻塞）、未配置不拉取。真实 VIGIL_HOME（tmp），零网络。
"""

from __future__ import annotations

import json

import pytest

import tools.pricing as pricing


def _reset_pricing_cache():
    pricing._pricing_cache = {"path": None, "mtime": None, "size": None, "data": None}


@pytest.fixture(autouse=True)
def env_home(tmp_path, monkeypatch):
    import hermes_cli.config as hc

    monkeypatch.setenv("VIGIL_HOME", str(tmp_path))
    hc._LOAD_CONFIG_CACHE.clear()
    _reset_pricing_cache()
    yield tmp_path
    hc._LOAD_CONFIG_CACHE.clear()
    _reset_pricing_cache()


def _write_pricing(tmp_path, prices: dict, extra: dict | None = None):
    import yaml

    data = {"schema_version": 1, "updated_at": "2026-08-23T00:00:00Z", "prices": prices}
    if extra:
        data.update(extra)
    (tmp_path / "pricing.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_manual_over_online_over_builtin(tmp_path):
    _write_pricing(
        tmp_path,
        {
            "m-test": {
                "input_per_1m": 0.1,
                "output_per_1m": 0.2,
                "currency": "usd",
                "source": "manual",
            },
            "m-online": {
                "input_per_1m": 1.0,
                "output_per_1m": 2.0,
                "currency": "usd",
                "source": "online",
                "fetched_at": "2026-08-23T10:00:00Z",
            },
        },
    )
    price = pricing.get_model_price("m-test")
    assert price["input_per_1m"] == 0.1
    assert price["source"] == "manual"

    # 删掉 manual → online 兜底
    _write_pricing(tmp_path, {"m-test": {
        "input_per_1m": 1.0,
        "output_per_1m": 2.0,
        "currency": "usd",
        "source": "online",
        "fetched_at": "2026-08-23T10:00:00Z",
    }})
    price = pricing.get_model_price("m-test")
    assert price["input_per_1m"] == 1.0
    assert price["source"] == "online"

    # 空表 → 未知模型 None
    _write_pricing(tmp_path, {})
    assert pricing.get_model_price("m-test") is None


def test_currency_validation_skips_invalid(tmp_path):
    _write_pricing(
        tmp_path,
        {
            "m-bad": {
                "input_per_1m": 0.5,
                "output_per_1m": 1.0,
                "currency": "eur",
                "source": "manual",
            },
        },
    )
    assert pricing.get_model_price("m-bad") is None


def test_builtin_deepseek_v4_flash_uses_official_snapshot(tmp_path):
    # 零配置（无 pricing.yaml）→ builtin：usage_pricing 官方快照
    price = pricing.get_model_price("deepseek-v4-flash")
    assert price is not None
    assert price["source"] == "builtin"
    assert price["currency"] == "usd"
    assert price["input_per_1m"] == 0.14
    assert price["output_per_1m"] == 0.28
    assert price.get("pricing_version") == "deepseek-pricing-2026-07"


def test_builtin_estimate_table_fallback(tmp_path, monkeypatch):
    # 官方快照被清空（模拟无快照条目）→ 内置估算表（任务书 0.28/0.42）
    from agent import usage_pricing

    monkeypatch.setattr(usage_pricing, "_OFFICIAL_DOCS_PRICING", {})
    price = pricing.get_model_price("deepseek-v4-flash")
    assert price is not None
    assert price["source"] == "builtin"
    assert price["input_per_1m"] == 0.28
    assert price["output_per_1m"] == 0.42


def test_unknown_model_returns_none(tmp_path):
    assert pricing.get_model_price("no-such-model-xyz") is None
    assert pricing.get_model_price("") is None


def test_estimate_cost():
    price = {"input_per_1m": 0.14, "output_per_1m": 0.28, "currency": "usd", "source": "builtin"}
    assert pricing.estimate_cost(1000, 200, price) == pytest.approx(0.000196)
    assert pricing.estimate_cost(15248, 76, price) == pytest.approx(0.002156)
    assert pricing.estimate_cost(100, 100, None) is None


class _FakeHttpResp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._raw


def _openrouter_payload():
    return {
        "data": [
            {"id": "deepseek/deepseek-v4-flash", "pricing": {"prompt": "0.28", "completion": "0.42"}},
            {"id": "openai/gpt-5.6-luna", "pricing": {"prompt": "0.000001", "completion": "0.000006"}},
            {"id": "no-price-model", "pricing": {"prompt": None, "completion": None}},
        ]
    }


def test_fetch_openrouter_success_writes_cache_and_keeps_manual(tmp_path, monkeypatch):
    _write_pricing(
        tmp_path,
        {
            "m-manual": {
                "input_per_1m": 0.01,
                "output_per_1m": 0.02,
                "currency": "cny",
                "source": "manual",
            },
        },
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=8: _FakeHttpResp(_openrouter_payload()))

    count = pricing.fetch_openrouter_prices()
    assert count == 2  # 无价格模型跳过

    import yaml

    saved = yaml.safe_load((tmp_path / "pricing.yaml").read_text(encoding="utf-8"))
    prices = saved["prices"]
    # manual 保留，且不被覆盖
    assert prices["m-manual"]["source"] == "manual"
    assert prices["m-manual"]["input_per_1m"] == 0.01
    # online 条目带 fetched_at
    flash = prices["deepseek/deepseek-v4-flash"]
    assert flash["source"] == "online"
    assert flash["input_per_1m"] == pytest.approx(0.28 * 1_000_000)
    assert flash["output_per_1m"] == pytest.approx(0.42 * 1_000_000)
    assert flash["fetched_at"]
    assert "no-price-model" not in prices

    # 拉取后 online 缓存命中（builtin 之上、manual 之下）
    assert pricing.get_model_price("deepseek/deepseek-v4-flash")["source"] == "online"


def test_fetch_openrouter_failure_non_blocking(tmp_path, monkeypatch):
    def _boom(req, timeout=8):
        raise TimeoutError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    (tmp_path / "config.yaml").write_text(
        "ops:\n  pricing:\n    openrouter:\n      base_url: https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )
    result = pricing.try_refresh_pricing_online()
    assert result["refreshed"] is False
    assert "network down" in result["reason"]
    # 不抛异常、不写文件
    assert not (tmp_path / "pricing.yaml").exists()


def test_try_refresh_not_configured(tmp_path):
    (tmp_path / "config.yaml").write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    result = pricing.try_refresh_pricing_online()
    assert result == {"refreshed": False, "reason": "not_configured", "count": 0}
