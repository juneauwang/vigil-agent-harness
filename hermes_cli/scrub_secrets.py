"""``vigil security scrub`` — 清理 state.db 已落库的凭据明文（OPS-DELTA 批次三十二）。

背景：批三十二前，含敏感关键词的 clarify 问答（问题/答复含密码）以明文落
state.db（role=tool 消息，如 id 503）。本命令把 messages 表各文本列过一遍
redact（凭据值登记表精确打码 + 键名形态打码），改写后重建 FTS 索引。

用法：
    vigil security scrub                      # 扫描并清理（凭据值登记表来源）
    vigil security scrub --dry-run            # 只报告，不改写
    vigil security scrub --values abc --values def   # 额外追加待清理值
    vigil security scrub --session sess-xxx   # 只清理指定会话
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Optional

# messages 表中可能携带凭据明文的文本列（reasoning/tool_calls 等一并清理）。
_SCRUB_COLUMNS = (
    "content",
    "api_content",
    "reasoning",
    "reasoning_content",
    "tool_calls",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)


def _mask_text(value):
    """redact 单个文本列值：有变化返回打码后字符串，无变化返回 None。"""
    if value is None or not isinstance(value, str) or not value:
        return None
    try:
        from agent.redact import redact_sensitive_text
        masked = redact_sensitive_text(value, force=True, credential_values=True)
    except Exception:
        return None
    return masked if masked != value else None


def _fts_rebuild(conn) -> None:
    """重建 FTS 索引（与 hermes_state 修复路径同款：FTS5 'rebuild' 命令）。"""
    for table in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
        try:
            conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        except sqlite3.OperationalError:
            continue  # 表不存在 / tokenizer 缺失——跳过


def cmd_scrub_secrets(args) -> int:
    """扫描 state.db messages，打码已落库凭据明文，重建 FTS。返回退出码。"""
    dry_run = bool(getattr(args, "dry_run", False))
    session_id = getattr(args, "session", None) or None
    extra_values = list(getattr(args, "values", None) or [])
    if extra_values:
        try:
            from agent.redact import register_credential_value
            for v in extra_values:
                register_credential_value(v)
        except Exception as exc:
            print(f"额外值登记失败：{exc}", file=sys.stderr)

    from agent.redact import registered_credential_values
    known = registered_credential_values()
    if not known and not extra_values:
        print("凭据值登记表为空，且未提供 --values；没有可清理的目标值。")
        return 0

    from hermes_state import _default_db_path
    db_path = _default_db_path()
    if not db_path.is_file():
        print(f"state.db 不存在：{db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        where = "WHERE session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()
        rows = conn.execute(
            f"SELECT id, session_id FROM messages {where}", params
        ).fetchall()
        if not rows:
            print("messages 表为空，无需清理。")
            return 0

        total_updated = 0
        column_updates = {col: 0 for col in _SCRUB_COLUMNS}
        for row in rows:
            msg_id = row["id"]
            changes = {}
            for col in _SCRUB_COLUMNS:
                cur = conn.execute(
                    f"SELECT {col} FROM messages WHERE id = ?", (msg_id,)
                ).fetchone()
                masked = _mask_text(cur[0] if cur else None)
                if masked is not None:
                    changes[col] = masked
            if not changes:
                continue
            total_updated += 1
            for col, masked in changes.items():
                column_updates[col] += 1
            if not dry_run:
                sets = ", ".join(f"{c} = ?" for c in changes)
                conn.execute(
                    f"UPDATE messages SET {sets} WHERE id = ?",
                    (*changes.values(), msg_id),
                )

        conn.commit()
        if not dry_run and total_updated:
            _fts_rebuild(conn)
            conn.commit()

        action = "待清理" if dry_run else "已清理"
        print(f"{action} {total_updated} 条消息（登记表值 {len(known)} 个"
              + (f"，会话 {session_id}" if session_id else "") + "）：")
        for col, n in column_updates.items():
            if n:
                print(f"  {col}: {n} 处")
        if dry_run and total_updated:
            print("（--dry-run：未改写；去掉该参数执行实际清理）")
        return 0
    finally:
        conn.close()
