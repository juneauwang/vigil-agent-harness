"""Topology (CMDB) tools for the Ops Agent Harness.

Implements the two-layer topology schema from ``ops-agent-harness.md`` §2.2:

  topology.yaml            — layer 1: environments + core_entities + key_paths
  entities/<name>.yaml     — layer 2: per-entity full profile (loaded on demand)

Files live under the active ``HERMES_HOME``.  Runtime state (CPU/pods/alerts)
is deliberately NOT stored here — the table only keeps the desired state.

Data contract (schema v0.1): see ops-agent-harness.md §2.2.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_TOPO_FILENAME = "topology.yaml"
_ENTITIES_DIRNAME = "entities"

# source=agent writes carry this tag (topo_update data contract).
_SOURCE_AGENT = "agent"
# Entities whose last_verified is older than this are reported as stale.
_STALE_DAYS = 30
# Known top-level schema fields that topo_update may rewrite directly; every
# other key lands in the entity's ``attrs`` map.
_TOPLEVEL_UPDATE_FIELDS = frozenset(
    {"env", "type", "endpoint", "owner", "status", "healthcheck", "depends_on", "depended_by"}
)

_DEFAULT_TOPO_SCHEMA = {
    "name": "topo_query",
    "description": (
        "查询运维拓扑表（平台 CMDB 事实层）。按实体名 / 类型 / 环境过滤；"
        "detail=True 时按需加载该实体的第二层完整档案（依赖关系、健康检查等）。"
        "执行任何运维操作前，先用本工具确认目标实体在拓扑表中的身份和环境；"
        "跨环境操作默认拒绝。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "description": "实体名（如 harbor）。省略时返回总览或按类型/环境过滤后的列表。",
            },
            "type": {
                "type": "string",
                "description": "按实体类型过滤（registry / service / db / gateway / ingress ...）。",
            },
            "env": {
                "type": "string",
                "description": "按环境过滤（prod / test / uat ...）。",
            },
            "detail": {
                "type": "boolean",
                "description": "是否加载第二层详细档案（默认 false，只返回第一层）。",
            },
        },
        "required": [],
    },
}

_DEFAULT_TOPO_UPDATE_SCHEMA = {
    "name": "topo_update",
    "description": (
        "更新拓扑表实体档案（状态/版本/属性）。自动携带 source=agent 与 last_verified=今天；"
        "修改 PROD 环境实体前需要人工审批确认。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "description": "要更新的实体名（必须已存在于拓扑表第一层）。",
            },
            "updates": {
                "type": "object",
                "description": (
                    "要写入的字段。env/type/endpoint/owner/status/healthcheck/depends_on/"
                    "depended_by 为顶层字段，其余键写入 attrs。"
                ),
            },
            "reason": {
                "type": "string",
                "description": "变更原因（审批展示与审计用）。",
            },
        },
        "required": ["entity", "updates"],
    },
}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _normalize(obj: Any) -> Any:
    """Recursively convert YAML scalars (dates/datetimes) to JSON-safe values."""
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj


def _hermes_home() -> Path:
    """Active HERMES_HOME as a Path (profile-scoped topology storage)."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _topology_path(home: Optional[Path] = None) -> Path:
    return (home or _hermes_home()) / _TOPO_FILENAME


def _safe_entity_path(home: Path, detail: str) -> Optional[Path]:
    """Resolve a layer-2 detail path, rejecting traversal outside HERMES_HOME."""
    if not detail or not isinstance(detail, str):
        return None
    root = home.resolve()
    candidate = (home / detail).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        logger.warning("topo: detail path escapes HERMES_HOME: %r", detail)
        return None
    return candidate


def load_topology(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load and validate layer 1 (topology.yaml). Returns None when absent/broken."""
    path = _topology_path(home)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("topo: failed to parse %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return _normalize(data)


def _all_core_entities(topo: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Merge environments' core_entities into the top-level core_entities list."""
    entities = list(topo.get("core_entities") or [])
    for env in topo.get("environments") or []:
        for name in env.get("core_entities") or []:
            if not any(e.get("name") == name for e in entities):
                entities.append({"name": name, "env": env.get("name", "")})
    return [e for e in entities if isinstance(e, dict)]


def _resolve_entity_detail(home: Path, entity: Dict[str, Any]) -> Optional[Path]:
    """Layer-2 path for an entity: explicit ``detail`` field, else entities/<name>.yaml."""
    detail = entity.get("detail")
    if detail:
        return _safe_entity_path(home, detail)
    name = entity.get("name", "")
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    return (home / _ENTITIES_DIRNAME / f"{name}.yaml").resolve()


def _load_entity_file(home: Path, entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = _resolve_entity_detail(home, entity)
    if path is None or not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("topo: failed to parse %s: %s", path, exc)
        return None
    if isinstance(data, dict):
        data = _normalize(data)
        data.setdefault("name", entity.get("name"))
        data.setdefault("env", entity.get("env"))
        data.setdefault("source", entity.get("source"))
        data.setdefault("last_verified", entity.get("last_verified"))
    return data


def _stale_flag(iso_date: Optional[str]) -> bool:
    """True when last_verified is older than _STALE_DAYS (or unparseable)."""
    if not iso_date:
        return True
    try:
        verified = _dt.date.fromisoformat(str(iso_date)[:10])
    except ValueError:
        return True
    return (_dt.date.today() - verified).days > _STALE_DAYS


def _today() -> str:
    return _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# topo_query
# ---------------------------------------------------------------------------

def topo_query(
    entity: Optional[str] = None,
    entity_type: Optional[str] = None,
    env: Optional[str] = None,
    detail: bool = False,
    home: Optional[Path] = None,
) -> str:
    """Query the topology table. Returns a JSON string (tool contract)."""
    home = home or _hermes_home()
    topo = load_topology(home)
    if topo is None:
        return tool_error(
            f"拓扑表不存在或无法解析: {_topology_path(home)}。"
            "运维会话需要先创建 topology.yaml（schema v0.1，见 ops-agent-harness.md §2.2）。"
        )

    if entity:
        entities = _all_core_entities(topo)
        match = next((e for e in entities if e.get("name") == entity), None)
        if match is None:
            return tool_error(f"拓扑表中不存在实体: {entity}")
        result = dict(match)
        result["_env_ref"] = _env_for_entity(topo, match)
        if detail:
            layer2 = _load_entity_file(home, match)
            if layer2 is not None:
                result["detail"] = layer2
            else:
                result["detail"] = None
                result["_detail_missing"] = True
        result["stale"] = _stale_flag(result.get("last_verified"))
        return json.dumps(_strip_internal(result), ensure_ascii=False, indent=2)

    entities = _all_core_entities(topo)
    if entity_type:
        entities = [e for e in entities if e.get("type") == entity_type]
    if env:
        entities = [e for e in entities if e.get("env") == env]
    for e in entities:
        e.setdefault("stale", _stale_flag(e.get("last_verified")))
    return json.dumps(
        {"count": len(entities), "entities": entities}, ensure_ascii=False, indent=2
    )


def _env_for_entity(topo: Dict[str, Any], entity: Dict[str, Any]) -> Optional[str]:
    for env in topo.get("environments") or []:
        if entity.get("name") in (env.get("core_entities") or []):
            return env.get("name")
    return entity.get("env")


def _strip_internal(result: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# topo_update
# ---------------------------------------------------------------------------

def _entity_env(topo: Dict[str, Any], entity: Dict[str, Any]) -> str:
    return _env_for_entity(topo, entity) or entity.get("env") or ""


def topo_update(
    entity: str,
    updates: Dict[str, Any],
    reason: str = "",
    home: Optional[Path] = None,
) -> str:
    """Update a topology entity. Writes source=agent + last_verified=today.

    PROD entities require human approval before the write (request_tool_approval).
    """
    home = home or _hermes_home()
    topo = load_topology(home)
    if topo is None:
        return tool_error("拓扑表不存在或无法解析，无法更新。")

    if not isinstance(updates, dict) or not updates:
        return tool_error("updates 必须是非空对象。")

    entities = _all_core_entities(topo)
    match = next((e for e in entities if e.get("name") == entity), None)
    if match is None:
        return tool_error(f"拓扑表中不存在实体: {entity}")

    env_name = _entity_env(topo, match)
    target_path = _resolve_entity_detail(home, match)
    if target_path is None:
        return tool_error(f"实体 {entity} 的 detail 路径非法（越界 HERMES_HOME）。")

    # PROD 变更 → 审批确认（钉在执行工具层，LLM 无法绕过）。
    if env_name == "prod" or env_name.startswith("prod"):
        from tools.approval import request_tool_approval
        approval = request_tool_approval(
            "topo_update",
            f"修改 PROD 拓扑实体 {entity}（{updates}）需要审批确认",
            rule_key=f"topo_update:prod:{entity}",
        )
        if not approval.get("approved"):
            return tool_error(
                approval.get("message") or "PROD 拓扑变更未获审批，已取消。",
                approved=False,
            )

    existing: Dict[str, Any] = {}
    if target_path.is_file():
        try:
            existing = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        except Exception as exc:
            return tool_error(f"实体档案解析失败: {target_path} ({exc})")

    for key, value in updates.items():
        if key in _TOPLEVEL_UPDATE_FIELDS:
            existing[key] = value
        else:
            attrs = existing.setdefault("attrs", {})
            if not isinstance(attrs, dict):
                attrs = {}
                existing["attrs"] = attrs
            attrs[key] = value

    existing["source"] = _SOURCE_AGENT
    existing["last_verified"] = _today()
    existing.setdefault("name", entity)
    existing.setdefault("env", env_name)

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception as exc:
        return tool_error(f"写入拓扑实体失败: {exc}")

    audit = {
        "entity": entity,
        "env": env_name,
        "updates": updates,
        "reason": reason,
        "source": _SOURCE_AGENT,
        "last_verified": _today(),
    }
    return json.dumps({"status": "updated", **audit}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Availability check + registry
# ---------------------------------------------------------------------------

def _ops_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}) or {}
    except Exception:
        return {}


def check_topo_requirements() -> bool:
    """Tools are gated on the ops topology being enabled in config.yaml."""
    return bool(_ops_config().get("topology", {}).get("enabled", False))


def _query_handler(args: Dict[str, Any], **kwargs) -> str:
    return topo_query(
        entity=args.get("entity"),
        entity_type=args.get("type"),
        env=args.get("env"),
        detail=bool(args.get("detail", False)),
    )


def _update_handler(args: Dict[str, Any], **kwargs) -> str:
    return topo_update(
        entity=args.get("entity", ""),
        updates=args.get("updates") or {},
        reason=args.get("reason", ""),
    )


registry.register(
    name="topo_query",
    toolset="topo",
    schema=_DEFAULT_TOPO_SCHEMA,
    handler=_query_handler,
    check_fn=check_topo_requirements,
    emoji="🗺️",
    max_result_size_chars=30_000,
)

registry.register(
    name="topo_update",
    toolset="topo",
    schema=_DEFAULT_TOPO_UPDATE_SCHEMA,
    handler=_update_handler,
    check_fn=check_topo_requirements,
    emoji="✏️",
    max_result_size_chars=30_000,
)
