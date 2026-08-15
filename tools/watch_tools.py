"""Vigil 值守分析层入口（OPS-DELTA #9 第二层）：``watch_digest`` 消费 inbox。

采集/分析分层的第二层——**需要 LLM**：agent 会话里调用 ``watch_digest()`` 读
``~/.vigil/watch/inbox/`` 未处理条目 → 紧凑摘要（alertname/severity/instance/
采集时间 + 拓扑关联提示：instance 命中拓扑实体给出实体名/env，未命中标注
"不在拓扑"）→ agent 据此做分级结论/播报/runbook 匹配。session 关闭时队列
堆积，下次启动补处理（产品文档"回来补拉"形态，不是缺陷是设计）。

数据契约（docstring 即契约）：
  - 默认只读不消费：``watch_digest()`` 返回摘要，不动 ``processed`` 标记；
  - 消费语义显式触发：``watch_digest(mark_processed=True)`` 在返回摘要后把
    当前所有未处理条目标记为 ``processed: true``（agent 播报/处置完成后调用，
    避免误消费）。已处理的条目不再出现在摘要里。

门控：``ops.watch.enabled: false`` → check_fn 返回 False，工具不出现
（硬约束 3，与 prom toolset 同策略）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry
from tools.watch_collect import _iter_inbox, watch_enabled

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbox reading
# ---------------------------------------------------------------------------

def _unprocessed_entries() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for path in _iter_inbox():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("processed"):
            continue
        data["_path"] = str(path)
        entries.append(data)
    return entries


def _mark_processed(paths: List[str]) -> None:
    """把指定 inbox 条目标记 processed: true（原子重写，best-effort）。"""
    for path_str in paths:
        path = Path(path_str)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["processed"] = True
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as exc:
            logger.warning("watch_digest: mark processed %s failed: %s", path_str, exc)


# ---------------------------------------------------------------------------
# Topology association
# ---------------------------------------------------------------------------

def _topo_match(instance: str) -> Optional[Dict[str, str]]:
    """instance → 拓扑实体名/env（best-effort；拓扑缺失/未命中返回 None）。

    命中规则：instance == 实体名/endpoint，或 instance 以 "name:"/"endpoint:"
    前缀（instance:port 形态）。跨层（host/cross_host/第二层服务）统一经
    ``_all_core_entities`` 扁平视图查找。
    """
    inst = (instance or "").strip()
    if not inst:
        return None
    try:
        from tools.topo_tools import _all_core_entities, _entity_env, load_topology
        topo = load_topology()
        if topo is None:
            return None
        for ent in _all_core_entities(topo):
            name = ent.get("name") or ""
            endpoint = ent.get("endpoint") or ""
            if inst == name or inst.startswith(f"{name}:"):
                return {"name": name, "env": _entity_env(topo, ent)}
            if endpoint and (inst == endpoint or inst.startswith(f"{endpoint.rstrip('/')}:")):
                return {"name": name, "env": _entity_env(topo, ent)}
    except Exception as exc:
        logger.debug("watch_digest: topology lookup failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

def watch_digest(mark_processed: bool = False) -> str:
    """读 watch inbox 未处理条目 → 紧凑摘要（含拓扑关联）。无未处理 → 明确返回。"""
    entries = _unprocessed_entries()
    if not entries:
        return "无待处理告警。"

    lines: List[str] = []
    total = 0
    for entry in entries:
        for alert in entry.get("alerts") or []:
            if not isinstance(alert, dict):
                continue
            total += 1
            name = alert.get("alertname") or "?"
            severity = alert.get("severity") or "-"
            instance = alert.get("instance") or "-"
            collected = entry.get("collected_at") or "-"
            match = _topo_match(instance)
            if match:
                topo_note = f"拓扑: {match['name']} ({match['env'] or 'env?'})"
            else:
                topo_note = "拓扑: 不在拓扑"
            lines.append(
                f"- [{severity}] {name} · instance={instance} · 采集于 {collected} · {topo_note}"
            )

    if not lines:
        return "无待处理告警。"

    head = f"待处理告警: {total} 条（inbox 未处理 {len(entries)} 个采集条目）"
    result = head + "\n" + "\n".join(lines)
    if mark_processed:
        _mark_processed([e["_path"] for e in entries])
        result += (
            "\n（已按 mark_processed=True 把上述条目标记为 processed，"
            "下次调用不再出现。）"
        )
    return result


def check_watch_requirements() -> bool:
    """watch_digest 可用性门控：``ops.watch.enabled`` 显式 false → 关闭。

    缺省（无该键）→ 可用（工具只读本地 inbox，零网络 footprint）；与 prom
    toolset 同策略：不注册进 _VIGIL_CORE_TOOLS，用户显式启用 toolset 才有
    该工具。
    """
    return watch_enabled()


# ---------------------------------------------------------------------------
# Schema + registry
# ---------------------------------------------------------------------------

_DEFAULT_DIGEST_SCHEMA = {
    "name": "watch_digest",
    "description": (
        "读值守采集 inbox（~/.vigil/watch/inbox/，vigil-watch 常驻服务写入）的"
        "未处理告警条目 → 紧凑摘要：alertname/severity/instance/采集时间 + 拓扑"
        "关联（instance 命中拓扑实体给出实体名/env，未命中标注「不在拓扑」）。"
        "值守层第二层：agent 据此做分级结论/播报/runbook 匹配。契约：默认只读"
        "不消费；播报/处置完成后以 mark_processed=True 再调一次，条目标记 "
        "processed 后不再出现。无未处理 → 返回「无待处理告警」。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mark_processed": {
                "type": "boolean",
                "description": (
                    "True = 返回摘要后把当前未处理条目标记已消费"
                    "（agent 播报/处置完成后调用，避免误消费）。"
                ),
            },
        },
    },
}


def _digest_handler(args: Dict[str, Any], **kwargs) -> str:
    return watch_digest(mark_processed=bool(args.get("mark_processed", False)))


registry.register(
    name="watch_digest",
    toolset="watch",
    schema=_DEFAULT_DIGEST_SCHEMA,
    handler=_digest_handler,
    check_fn=check_watch_requirements,
    emoji="📥",
    max_result_size_chars=30_000,
)
