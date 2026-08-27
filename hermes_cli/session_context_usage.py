"""会话 context 使用率计算（batch81，OPS-DELTA #96）。

web_server /api/sessions 与 chat_api /api/chat/sessions 共用。used_tokens 口径：
取 messages 表 active=1（活 context）的 token_count 累计——compaction 归档行
已翻 active=0 不计，比 session_model_usage 的累计 API 用量更贴近"当前 context
塞了多少"。token_count 由持久化路径（run_agent._flush_messages_to_session_db）
逐条写入估算值（本批补齐：此前该列从未被写入，全是 NULL）；历史会话（本批之前
落库、无 token_count）区分不出"真空"与"未计量"——返回 None，前端显示"用量未知"
不瞎猜。活会话（web chat 注册表）另有 compressor.last_prompt_tokens（上次真实
API prompt tokens，CLI 状态栏同源）作兜底。

limit_tokens 三级解析：config 显式 ``model.context_length`` > 内置默认表
（agent.model_metadata.DEFAULT_CONTEXT_LENGTHS，最长键优先子串匹配）> None
（UI 显示"用量未知"）。纯本地计算，不做网络探测——列表页每次请求都要快。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_model_context_limit(model: Optional[str]) -> Optional[int]:
    """模型 context 上限三级解析：config 显式 > 内置默认表 > None。"""
    if model:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
            model_cfg = cfg.get("model") or {}
            if isinstance(model_cfg, dict):
                explicit = model_cfg.get("context_length")
                if isinstance(explicit, int) and explicit > 0:
                    return explicit
        except Exception:
            pass
        try:
            from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
            m = str(model).strip().lower()
            for key, length in sorted(
                DEFAULT_CONTEXT_LENGTHS.items(), key=lambda x: len(x[0]), reverse=True
            ):
                if key in m:
                    return length
        except Exception:
            pass
    return None


def context_usage_for(model: Optional[str], used_tokens: int) -> Dict[str, Any]:
    """组装 context_usage 字段：{used_tokens, limit_tokens, pct, model}。

    pct = round(used/limit*100)；limit 解析不到 → pct=None；used_tokens 为
    None（未计量）→ pct=None（前端显示"—"）。
    """
    limit = resolve_model_context_limit(model)
    pct = None
    used = used_tokens
    if used is not None:
        used = int(used)
    if limit and used is not None:
        pct = round(used * 100 / limit)
        pct = max(0, min(pct, 999))
    return {
        "used_tokens": used,
        "limit_tokens": limit,
        "pct": pct,
        "model": model,
    }


def session_used_tokens(session_db: Any, session_id: str) -> Optional[int]:
    """会话当前 context 用量：messages 表 active=1 的 token_count 累计。

    返回 None 表示"有消息但未计量"（本批之前落库的历史行 token_count 为 NULL，
    不能当作 0——那会把满 context 误显示成空）；真空会话（无消息）返回 0。
    """
    if session_db is None or not session_id:
        return 0
    try:
        cur = session_db._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(token_count), 0) FROM messages "
            "WHERE session_id = ? AND active = 1",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return 0
        count = int(row[0] or 0)
        total = int(row[1] or 0)
        if count == 0:
            return 0
        if total <= 0:
            return None
        return total
    except Exception:
        return None


def live_prompt_tokens(agent: Any) -> Optional[int]:
    """活会话兜底：compressor.last_prompt_tokens（上次真实 API prompt tokens）。

    压缩后该值停在 -1 哨兵（awaiting_real_usage_after_compression），当作
    未计量返回 None——压缩刚完成 context 已被重置，不误报也不虚报。
    """
    try:
        compressor = getattr(agent, "context_compressor", None)
        if compressor is None:
            return None
        tokens = getattr(compressor, "last_prompt_tokens", 0) or 0
        if tokens < 0:
            return None
        return int(tokens)
    except Exception:
        return None
