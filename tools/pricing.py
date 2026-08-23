"""LLM 价格三级来源（OPS-DELTA #79）。

存储：``~/.vigil/pricing.yaml``（与 matrix.yaml 同哲学——可演进数据独立文件、
可编辑、diff 清晰）。结构见任务书：``schema_version / updated_at / prices``，
每模型带 ``input_per_1m / output_per_1m / currency(usd|cny) / source
(manual|online|builtin) / fetched_at``。

三级来源优先级（高→低）：
1. manual  —— pricing.yaml 中 ``source: manual`` 的手写价（用户对实际支付价——
   DeepSeek 官方人民币价、公司协议价等——有最终发言权，永不被在线拉取覆盖）。
2. online  —— pricing.yaml 中 ``source: online`` 的在线拉取缓存（配置
   ``ops.pricing.openrouter.base_url`` 后由 web 启动后台线程拉取一次；
   拉取失败不阻塞，用现有缓存，下次再试）。
3. builtin —— 代码内置兜底：
   a. 复用 ``agent/usage_pricing`` 官方快照（provider 已知时最准；如
      deepseek-v4-flash 0.14/0.28 USD）；仅走官方快照/未知路由分支，绝不触发
      OpenRouter 等需网络的 models API——``get_model_price`` 永不做网络。
   b. 内置估算表（任务书给定 deepseek-v4-flash 0.28/0.42 USD 等）——标注
      "估算值，以实际账单为准"；仅当官方快照也查不到时兜底。

不引入汇率换算：usd/cny 各模型自带，显示原币种符号（$ / ¥）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)

_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_VALID_CURRENCIES = {"usd", "cny"}
_VALID_SOURCES = {"manual", "online", "builtin"}

# 内置估算表（source: builtin，第三级兜底）：任务书给定 deepseek-v4-flash
# 0.28/0.42 USD（OpenRouter 口径）。标注估算性质——仅当 usage_pricing 官方
# 快照也查不到时才命中（deepseek-v4-flash 实际走快照 0.14/0.28）。
_BUILTIN_ESTIMATES: Dict[str, tuple] = {
    "deepseek-v4-flash": (0.28, 0.42, "usd"),
}

# pricing.yaml 读取缓存：按 mtime/size 失效，避免每次请求都读盘。
_cache_lock = threading.Lock()
_pricing_cache: Dict[str, Any] = {"path": None, "mtime": None, "size": None, "data": None}


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def pricing_path() -> Path:
    """``~/.vigil/pricing.yaml``（profile-aware）。"""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "pricing.yaml"


def _read_pricing_file() -> Dict[str, Any]:
    """读 + 校验 pricing.yaml；任何异常 → 空 prices（不抛，零阻塞）。

    返回归一化结构 ``{"schema_version": int, "updated_at": str|None, "prices": {model: entry}}``，
    非法条目（缺字段 / currency 枚举外 / 非正数）跳过并记 debug。
    """
    path = pricing_path()
    try:
        mtime = path.stat().st_mtime
        size = path.stat().st_size
    except OSError:
        mtime = size = None
    with _cache_lock:
        cached = _pricing_cache
        if (
            cached["path"] == str(path)
            and cached["mtime"] == mtime
            and cached["size"] == size
            and cached["data"] is not None
        ):
            return cached["data"]

    data: Dict[str, Any] = {"schema_version": 1, "updated_at": None, "prices": {}}
    if mtime is not None:
        try:
            import yaml

            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            prices_raw = raw.get("prices")
            if isinstance(prices_raw, dict):
                for model, entry in prices_raw.items():
                    if not isinstance(entry, dict):
                        continue
                    in_price = _to_float(entry.get("input_per_1m"))
                    out_price = _to_float(entry.get("output_per_1m"))
                    currency = str(entry.get("currency") or "").strip().lower()
                    source = str(entry.get("source") or "").strip().lower()
                    if in_price is None or out_price is None:
                        continue
                    if currency not in _VALID_CURRENCIES:
                        _log.debug("pricing.yaml: 跳过 %s（currency=%r 非法）", model, entry.get("currency"))
                        continue
                    if source not in _VALID_SOURCES:
                        source = "builtin"
                    data["prices"][str(model)] = {
                        "input_per_1m": in_price,
                        "output_per_1m": out_price,
                        "currency": currency,
                        "source": source,
                        "fetched_at": str(entry.get("fetched_at") or "") or None,
                    }
            data["updated_at"] = str(raw.get("updated_at") or "") or None
            if isinstance(raw.get("schema_version"), int):
                data["schema_version"] = raw["schema_version"]
        except Exception as exc:
            _log.warning("pricing.yaml 读取/校验失败，回退空表: %s", exc)

    with _cache_lock:
        _pricing_cache.update(path=str(path), mtime=mtime, size=size, data=data)
    return data


def _config_default_provider() -> str:
    """config.yaml ``model.provider``（chat agent 实际路由；空 = 未知）。"""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        m = cfg.get("model") or {}
        if isinstance(m, dict):
            return str(m.get("provider") or "").strip()
        return ""
    except Exception:
        return ""


def _scan_official_snapshot(model: str) -> Optional[Dict[str, Any]]:
    """在 usage_pricing 官方快照中按模型名精确扫（provider 未知时兜底）。

    只读访问，dict 顺序确定性（deepseek 快照先于 fireworks，同名模型取前者）。
    绝不触发任何网络路径（快照是模块内静态 dict）。
    """
    try:
        from agent import usage_pricing

        snapshot = getattr(usage_pricing, "_OFFICIAL_DOCS_PRICING", None)
        if not snapshot:
            return None
        target = (model or "").lower().strip()
        for (provider, snap_model), entry in snapshot.items():
            if snap_model.lower() != target:
                continue
            in_price = entry.input_cost_per_million
            out_price = entry.output_cost_per_million
            if in_price is None or out_price is None:
                continue
            return {
                "input_per_1m": float(in_price),
                "output_per_1m": float(out_price),
                "currency": "usd",
                "source": "builtin",
                "fetched_at": entry.fetched_at.isoformat() if entry.fetched_at else None,
                "pricing_version": entry.pricing_version,
            }
    except Exception:
        _log.debug("official snapshot scan failed", exc_info=True)
    return None


def _builtin_price(model: str, provider: Optional[str]) -> Optional[Dict[str, Any]]:
    """内置兜底：usage_pricing 官方快照 → 内置估算表。零网络。"""
    entry: Optional[Dict[str, Any]] = None
    try:
        from agent import usage_pricing

        resolved_provider = (provider or "").strip() or _config_default_provider()
        if resolved_provider:
            route = usage_pricing.resolve_billing_route(model, provider=resolved_provider)
            # official_models_api（OpenRouter/Nous）走在线 models API——
            # get_model_price 永不做网络，跳过，交给扫描兜底。
            if route.billing_mode != "official_models_api":
                snap = usage_pricing.get_pricing_entry(model, provider=resolved_provider)
                if snap and snap.input_cost_per_million is not None and snap.output_cost_per_million is not None:
                    entry = {
                        "input_per_1m": float(snap.input_cost_per_million),
                        "output_per_1m": float(snap.output_cost_per_million),
                        "currency": "usd",
                        "source": "builtin",
                        "fetched_at": snap.fetched_at.isoformat() if snap.fetched_at else None,
                        "pricing_version": snap.pricing_version,
                    }
    except Exception:
        _log.debug("usage_pricing lookup failed", exc_info=True)
    if entry is None:
        entry = _scan_official_snapshot(model)
    if entry is None:
        est = _BUILTIN_ESTIMATES.get((model or "").lower().strip())
        if est:
            entry = {
                "input_per_1m": est[0],
                "output_per_1m": est[1],
                "currency": est[2],
                "source": "builtin",
                "fetched_at": None,
            }
    return entry


def get_model_price(model: str, provider: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """三级价格查找（高→低：manual → online → builtin）。未找到 → None。

    返回 ``{input_per_1m, output_per_1m, currency, source, fetched_at}``
    （builtin 附带 ``pricing_version``）。永不做网络请求。
    """
    name = (model or "").strip()
    if not name:
        return None
    prices = _read_pricing_file().get("prices") or {}
    for source in ("manual", "online"):
        entry = prices.get(name)
        if entry and entry.get("source") == source:
            return dict(entry)
    file_builtin = prices.get(name)
    if file_builtin and file_builtin.get("source") == "builtin":
        return dict(file_builtin)
    return _builtin_price(name, provider)


def estimate_cost(input_tokens: int, output_tokens: int, price: Optional[Dict[str, Any]]) -> Optional[float]:
    """``cost = input/1e6*in + output/1e6*out``；价格缺失 → None。"""
    if not price:
        return None
    try:
        cost = (
            int(input_tokens or 0) / 1_000_000 * float(price["input_per_1m"])
            + int(output_tokens or 0) / 1_000_000 * float(price["output_per_1m"])
        )
    except (KeyError, TypeError, ValueError):
        return None
    return round(cost, 6)


def _write_pricing_file(data: Dict[str, Any]) -> None:
    """原子写 pricing.yaml（临时文件 + os.replace），失败不抛。"""
    try:
        import yaml

        path = pricing_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        with _cache_lock:
            _pricing_cache.update(path=str(path), mtime=None, size=None, data=None)
    except Exception as exc:
        _log.warning("pricing.yaml 写入失败: %s", exc)


def fetch_openrouter_prices(base_url: Optional[str] = None) -> int:
    """拉取 OpenRouter ``/models`` 公开列表 → 合并写 pricing.yaml（source: online）。

    - 无需 API key（models 列表公开）。
    - 只更新/新增 online 条目；manual 条目永不被覆盖。
    - 任何异常向上抛（由 ``try_refresh_pricing_online`` 兜底为不阻塞）。

    返回写入的模型数。
    """
    base = (base_url or _DEFAULT_OPENROUTER_BASE_URL).rstrip("/")
    req = urllib.request.Request(
        f"{base}/models",
        headers={"User-Agent": "vigil-agent/1.0", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    models = payload.get("data") or []

    existing = _read_pricing_file().get("prices") or {}
    merged = {
        model: dict(entry)
        for model, entry in existing.items()
        if entry.get("source") == "manual"
    }
    now = _now_iso_utc()
    count = 0
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        pricing = m.get("pricing") or {}
        in_price = _to_float(pricing.get("prompt"))
        out_price = _to_float(pricing.get("completion"))
        if not mid or in_price is None or out_price is None:
            continue
        merged[mid] = {
            "input_per_1m": round(in_price * 1_000_000, 6),
            "output_per_1m": round(out_price * 1_000_000, 6),
            "currency": "usd",
            "source": "online",
            "fetched_at": now,
        }
        count += 1

    _write_pricing_file(
        {"schema_version": 1, "updated_at": now, "prices": merged}
    )
    return count


def _online_base_url() -> str:
    """``ops.pricing.openrouter.base_url``；空 = 不拉取（零网络依赖）。"""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        return str(cfg.get("ops", {}).get("pricing", {}).get("openrouter", {}).get("base_url") or "").strip()
    except Exception:
        return ""


def try_refresh_pricing_online(force: bool = False) -> Dict[str, Any]:
    """在线拉取入口（仅明确调用时执行——web 启动后台线程触发一次）。

    未配置 → ``{"refreshed": False, "reason": "not_configured"}``；拉取失败 →
    ``refreshed: False``（绝不抛异常，绝不阻塞主流程）。
    """
    base = _online_base_url()
    if not base:
        return {"refreshed": False, "reason": "not_configured", "count": 0}
    try:
        count = fetch_openrouter_prices(base)
        return {"refreshed": True, "count": count, "base_url": base}
    except Exception as exc:
        _log.warning("pricing online refresh failed (non-blocking): %s", exc)
        return {"refreshed": False, "reason": str(exc), "count": 0}
