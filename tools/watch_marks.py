"""告警人工处置标记层（task33）：ack / clear 状态外置到 sqlite。

为什么状态**不能**写进 inbox 快照文件（task33 复现的四缺陷焊死此结论）：
采集层 ``tools/watch_collect.collect()`` 的去重只看**未处理**快照里的
alertname|instance，且"无活跃告警不写盘"（恢复不留痕）。把 ack 表现为
``processed=true`` → 下一轮采集视为新告警再写一条 → 页面又冒出来、✓ 丢失；
把 clear 表现为"移出 inbox" → 下一轮同一告警原样回来；告警恢复后旧快照
永久 active。因此 ack/clear 必须存进与外置状态库（``state.db``），
采集层与 inbox 快照文件零改动（硬约束 2）。

状态契约（单一事实来源，消费端 = ``/api/incidents``）：

- ``action='ack'``   → 该 (alert_key, episode) 显示 + "已确认"标记
- ``action='clear'`` → 该 (alert_key, episode) 的任何快照都不显示
- ``episode = startsAt``：startsAt 变（恢复后复发）= 新 episode，标记自动
  失效、告警重新出现（正确告警语义，不做 TTL / 不做时效对账）
- 幂等：同一 (alert_key, episode, action) 重复写 = UPSERT，不堆行
- 撤销：DELETE 该 (alert_key, episode) 的全部行

键定义单一来源 = ``tools.watch_collect._alert_key``（alertname|instance），
调用方禁止自行拼 key。读失败一律返回空/False（**绝不让 /api/incidents 因
marks 500**，与既有 try/except 兜底风格一致）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

VALID_ACTIONS = ("ack", "clear")

MarkKey = Tuple[str, str]


def alert_key_for(alertname: Optional[str], instance: Optional[str]) -> str:
    """alertname|instance → 单一来源的键（复用采集层的 ``_alert_key``）。"""
    from tools.watch_collect import _alert_key

    return _alert_key({"alertname": alertname, "instance": instance})


def episode_for(starts_at: Optional[str]) -> str:
    """episode 身份 = startsAt（缺省 '-'，与采集层缺省值对齐）。"""
    return str(starts_at) if starts_at else "-"


def _open_db(*, read_only: bool):
    from hermes_state import SessionDB, _default_db_path

    return SessionDB(db_path=Path(_default_db_path()), read_only=read_only)


def list_marks() -> Dict[MarkKey, str]:
    """全部有效标记：``{(alert_key, episode): 'ack' | 'clear'}``。

    ``clear`` 优先于 ``ack``（同 episode 两者并存时清除生效）。DB/表读不到
    → 空 dict，消费端点照常返回告警列表。
    """
    db = None
    try:
        db = _open_db(read_only=True)
        rows = db._conn.execute(
            "SELECT alert_key, episode, action FROM incident_marks"
        ).fetchall()
    except Exception as exc:
        logger.debug("watch_marks: read failed: %s", exc)
        return {}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    marks: Dict[MarkKey, str] = {}
    for row in rows:
        try:
            key = (str(row["alert_key"]), str(row["episode"]))
            action = str(row["action"])
        except (KeyError, IndexError, TypeError):
            continue
        # clear 优先于 ack（同 key 两者并存时清除生效，逐键判定）
        if action == "clear" or key not in marks:
            marks[key] = action
    return marks


def set_mark(
    alert_key: str,
    episode: str,
    action: str,
    actor: Optional[str] = None,
) -> bool:
    """UPSERT 一条标记（幂等，不堆行）。失败返回 False。"""
    if action not in VALID_ACTIONS:
        return False
    db = None
    try:
        db = _open_db(read_only=False)

        def _do(conn):
            conn.execute(
                "INSERT OR REPLACE INTO incident_marks "
                "(alert_key, episode, action, created_at, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                (alert_key, episode, action, time.time(), actor),
            )

        db._execute_write(_do)
        return True
    except Exception as exc:
        logger.warning("watch_marks: set %s %s/%s failed: %s", action, alert_key, episode, exc)
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def delete_mark(alert_key: str, episode: str, action: Optional[str] = None) -> bool:
    """撤销标记：``action`` 给定删该行，否则删该 (key, episode) 全部行。失败返回 False。"""
    db = None
    try:
        db = _open_db(read_only=False)

        def _do(conn):
            if action:
                conn.execute(
                    "DELETE FROM incident_marks "
                    "WHERE alert_key = ? AND episode = ? AND action = ?",
                    (alert_key, episode, action),
                )
            else:
                conn.execute(
                    "DELETE FROM incident_marks WHERE alert_key = ? AND episode = ?",
                    (alert_key, episode),
                )

        db._execute_write(_do)
        return True
    except Exception as exc:
        logger.warning("watch_marks: delete %s/%s failed: %s", alert_key, episode, exc)
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
