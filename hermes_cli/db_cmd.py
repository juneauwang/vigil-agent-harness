"""``vigil db`` — built-in read-only state.db inspection (batch 38 §AQ).

Vigil 排查自身状态（会话 status/ended_at/busy 标志、审批记录、runbook 等）需要
直接查库，但宿主机不一定有 sqlite3 CLI，系统 python 也可能太旧（§AQ / §AS D2）。
本命令封装 SessionDB 的 SQLite 连接（不依赖系统 sqlite3 二进制）：

- 只读：默认只放行 SELECT / EXPLAIN / PRAGMA；写语句在打开连接之前就拒绝，
  并给出风险提示。单语句约束（拒绝分号拼接），避免借 executescript 混入写操作。
- 输出不经凭据泄露：字符串单元格过 redact_sensitive_text（非凭据形态原文不动）。
"""

from __future__ import annotations

import sys
from typing import Optional

_READ_ONLY_PREFIXES = ("select", "explain", "pragma")


def cmd_db(args, db_parser=None):
    """``vigil db`` dispatch（由 main.py set_defaults 接线）。"""
    action = getattr(args, "db_action", None)
    if action == "query":
        _cmd_db_query(getattr(args, "sql", ""), limit=getattr(args, "limit", 50))
        return
    if db_parser is not None:
        db_parser.print_help()
    else:
        print("usage: vigil db query <SQL> [--limit N]")


def _reject_write(sql: str) -> Optional[str]:
    """返回 None 表示放行，否则返回拒绝原因（写库/多语句/空）。"""
    stmt = (sql or "").strip().rstrip(";").strip()
    if not stmt:
        return "empty SQL statement"
    if ";" in stmt:
        return "only a single statement is allowed (no semicolons)"
    lowered = stmt.lower()
    if not lowered.startswith(_READ_ONLY_PREFIXES):
        return (
            "write statements are rejected — this command is read-only "
            "(only SELECT / EXPLAIN / PRAGMA are allowed)"
        )
    return None


def _cmd_db_query(sql: str, *, limit: int = 50) -> None:
    reason = _reject_write(sql)
    if reason is not None:
        print(f"vigil db query: {reason}", file=sys.stderr)
        raise SystemExit(2)

    from hermes_state import DEFAULT_DB_PATH, SessionDB

    if not DEFAULT_DB_PATH.exists():
        print(f"No session database at {DEFAULT_DB_PATH} — nothing to query.")
        return

    db = SessionDB(read_only=True)
    try:
        with db._read_ctx() as conn:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(limit + 1)
    except Exception as exc:
        print(f"query failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        db.close()

    if not rows:
        print("(no rows)")
        return
    truncated = len(rows) > limit
    shown = rows[:limit]
    for row in shown:
        cells = []
        for value in row:
            if isinstance(value, str):
                value = _redact_cell(value)
            cells.append(str(value))
        print(" | ".join(cells))
    if truncated:
        print(f"... ({len(rows) - limit} more row(s); re-run with a narrower query or --limit)")


def _redact_cell(value: str, max_len: int = 240) -> str:
    """凭据形态值打码 + 单格截断；非凭据原文原样返回。"""
    try:
        from agent.redact import redact_sensitive_text

        value = redact_sensitive_text(value, force=True)
    except Exception:
        pass
    if len(value) > max_len:
        return value[:max_len] + "…"
    return value
