"""Topology (CMDB) tools for the Ops Agent Harness.

Implements the three-layer topology schema v0.2/v0.3 from
``ops-agent-harness.md`` §2.2 (OPS-DELTA #6 / #42):

  topology.yaml            — layer 1: environments + clusters + hosts + cross_host + key_paths
  hosts/<hostname>.yaml    — layer 2: per-host services index (one row per service)
  entities/<...>.yaml      — layer 3: per-service full profile (loaded on demand)

Schema v0.3 (OPS-DELTA #42) adds: ``cluster`` (three layers + top-level
``clusters:`` overview), fixed env enum ``local/test/dev/prod`` (legacy custom
names map by tier on read), L3 entity naming ``entities/{cluster}__{host}__{name}.yaml``
(cross-host name collisions), and ``credential`` references on host rows /
``ssh`` sections on entities (type/ref/user/port only — no plaintext secrets).

Read-compat is a hard requirement: v0.1 (flat ``core_entities``), v0.2
(hosts/cross_host, old ``entities/<name>.yaml`` paths) and v0.3 all load;
old files are never rewritten.  Writes only produce v0.3.

Files live under the active Vigil home.  Runtime state (CPU/pods/alerts)
is deliberately NOT stored here — the table only keeps the desired state.

Data contract (schema v0.2/v0.3, v0.1 compat): see ops-agent-harness.md §2.2.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from tools.topo_discovery import (
    _LOCAL_HOST_ALIASES,
    _build_local_runner,
    _build_ssh_runner,
    _parse_docker_ps,
    discover_host,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_TOPO_FILENAME = "topology.yaml"
_ENTITIES_DIRNAME = "entities"
_HOSTS_DIRNAME = "hosts"

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
        "查询运维拓扑表（平台 CMDB 事实层，三层模型 v0.3）。"
        "无参返回第一层总览（clusters + hosts + cross_host，紧凑）；host=<name> "
        "展开该主机的第二层服务索引；entity=<name> 跨层名解析（先服务名再 "
        "host/cross_host）；type=/env=/cluster= 过滤扁平视图（cluster 缺省 "
        "显示 default）；detail=True 时按需加载第三层完整档案（依赖关系、健康检查、"
        "ssh 连接引用等）。host 行的 credential 引用（type/ref/user/port）随行返回，"
        "port 缺省 22，密码明文不落拓扑。"
        "执行任何运维操作前，先用本工具确认目标实体在拓扑表中的身份和环境；"
        "跨环境操作默认拒绝。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {
                "type": "string",
                "description": "实体名（如 harbor）。省略时返回总览或按类型/环境/集群过滤后的列表。",
            },
            "type": {
                "type": "string",
                "description": "按实体类型过滤（registry / service / db / gateway / ingress ...）。",
            },
            "env": {
                "type": "string",
                "description": "按环境过滤（local / test / dev / prod 四值；老自定义名按档位映射）。",
            },
            "cluster": {
                "type": "string",
                "description": "按集群过滤（如 k3s-prod）；host 未标 cluster 时按 default 处理。",
            },
            "host": {
                "type": "string",
                "description": "按主机展开（v0.2/v0.3）：返回该 host 及其第二层服务索引。",
            },
            "detail": {
                "type": "boolean",
                "description": "是否加载第三层详细档案（默认 false，只返回索引/总览行）。",
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
        "状态变更（容器 stop/start 等）请先运行 topo_status_sync 检测差异，再确认同步；"
        "手动 topo_update 写 status 仅用于 topo_status_sync 无法确定的状态。"
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
                    "正例：{\"endpoint\": \"1.2.3.4\", \"owner\": \"x\"}；"
                    "反例：{\"attrs\": {\"endpoint\": \"...\"}}（应直接传 endpoint，"
                    "attrs 键会被自动展开合并，不需要包这一层）。"
                    "只接受标量属性值；dict/list 复杂结构会被拒绝（防嵌套污染）。"
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

_TOPO_DISCOVER_SCHEMA = {
    "name": "topo_discover",
    "description": (
        "SSH 自动发现主机拓扑（docker/k8s/systemd/端口/GPU）并返回 schema v0.3 "
        "片段。仅发现、不自动落盘（needs_review=true，dry_run 语义默认开启）；"
        "发现结果只是草案，未经确认不参与权限判定——下一步用 topo_query 查看待审"
        "实体、topo_update 逐条确认（修正名称/类型/endpoint 并置 needs_review=false）"
        "后实体才进入权威拓扑。SSH 凭据走 ssh-agent/私钥或既有保险箱+askpass "
        "注入，不接受明文密码参数。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "目标主机 IP/主机名（单台）。",
            },
            "env": {
                "type": "string",
                "description": "目标环境（local/test/dev/prod 四值；老自定义名按档位映射）。",
            },
            "cluster": {
                "type": "string",
                "description": "集群名（可选）；缺省显示 default，实体文件名按 env 兜底。",
            },
            "user": {
                "type": "string",
                "description": "SSH 用户，默认 root。",
            },
            "key": {
                "type": "string",
                "description": "SSH 私钥路径；缺省走 ssh-agent。",
            },
            "dry_run": {
                "type": "boolean",
                "description": "只预览发现片段，不落盘。工具场景默认 true。",
            },
            "force": {
                "type": "boolean",
                "description": "工具场景不自动落盘，此参数仅保留 CLI 对齐。",
            },
        },
        "required": ["host", "env"],
    },
}

_TOPO_STATUS_SYNC_SCHEMA = {
    "name": "topo_status_sync",
    "description": (
        "对比拓扑实体状态与实际容器状态，检测不一致（批次二十四 §R）。"
        "对 docker 容器实体（attrs.container / type=docker-container / "
        "attrs.runtime=docker）执行 docker ps -a 检查实际状态：实际状态可确定"
        "（running/exited 等）且与拓扑 status 不一致 → 差异 action=update；"
        "实际状态不确定（容器已删除/主机不可达/无 docker 权限）→ action=ask，"
        "留给用户确认。confirm=false（默认）只报告差异不落盘（dry-run）；"
        "confirm=true 对 action=update 的实体写 status（复用 topo_update 路径，"
        "PROD 实体仍走审批）。容器/服务状态变更后先运行本工具检测差异，再向用户"
        "确认同步；不要假设拓扑自动更新。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "主机名/IP（或实体名）；省略时检查全部 docker 容器实体。",
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "确认落盘：为 true 时对 action=update 的实体写入 status"
                    "（默认 false = dry-run，只报告差异不落盘）。"
                ),
            },
        },
        "required": [],
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
    """Resolve topology.yaml under the active home (no sibling fallback).

    OPS-DELTA #14 的 sibling ops profile 回退已删除：数据只挂在解析出的
    home 下（home/topology.yaml），无数据 → 调用方报"数据缺失"。
    """
    from tools.ops_data_home import resolve_ops_data_home
    home = home or _hermes_home()
    return resolve_ops_data_home(home, _TOPO_FILENAME) / _TOPO_FILENAME


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


def _is_v2_or_v3(topo: Dict[str, Any]) -> bool:
    """Schema version discriminator: v0.2/v0.3 (hosts/cross_host/clusters) vs v0.1.

    ``version: 2`` or ``version: 3`` wins; absent version falls back to shape
    detection (hosts/cross_host/clusters) so hand-written files without the
    field still load as layered when they use the new layout.
    """
    if topo.get("version") in (2, 3):
        return True
    if "version" in topo:
        return False
    return bool(topo.get("hosts") or topo.get("cross_host") or topo.get("clusters"))


def _load_host_index(home: Path, host: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Load a host's layer-2 services index (hosts/<name>.yaml by default).

    Honors an explicit ``services_index`` field (containment-checked against
    HERMES_HOME via ``_safe_entity_path`` — the new hosts/ directory is
    covered by the same traversal guard as entities/).
    """
    name = host.get("name") if isinstance(host, dict) else None
    if not name or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    index_field = host.get("services_index") if isinstance(host, dict) else None
    if index_field:
        path = _safe_entity_path(home, index_field)
    else:
        path = (home / _HOSTS_DIRNAME / f"{name}.yaml").resolve()
    if path is None or not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("topo: failed to parse host index %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return _normalize(data)


def _all_services(topo: Dict[str, Any], home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Layer-2 services across every host's services index (v0.2).

    Each service keeps its own name + env; a service row without an explicit
    env inherits its host's env (the "服务 → 所属 host → env" chain used by
    ops_target).  ``_host`` is an internal marker (stripped from query output)
    so env resolution and target matching can trace back to the host.
    """
    services: List[Dict[str, Any]] = []
    if home is None:
        return services
    for host in topo.get("hosts") or []:
        if not isinstance(host, dict):
            continue
        index = _load_host_index(home, host)
        if index is None:
            continue
        host_env = host.get("env") or index.get("env") or ""
        host_cluster = str(host.get("cluster") or index.get("cluster") or "default")
        for svc in index.get("services") or []:
            if not isinstance(svc, dict):
                continue
            row = dict(svc)
            if not row.get("env"):
                row["env"] = host_env
            row.setdefault("cluster", host_cluster)
            row["_host"] = host.get("name", "")
            services.append(row)
    return services


def _all_core_entities(topo: Dict[str, Any], home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The flat entity view both schemas resolve to.

    v0.1: top-level ``core_entities`` merged with each environment's
    ``core_entities`` (existing behavior, unchanged).
    v0.2: hosts + cross_host + layer-2 services (hosts need ``home`` to read
    the services indexes; pass None only when services are irrelevant).

    The permission matrix / runbook bind by name+env — both views keep that
    contract (层级是组织方式不是命名空间).
    """
    if not _is_v2_or_v3(topo):
        entities = list(topo.get("core_entities") or [])
        for env in topo.get("environments") or []:
            for name in env.get("core_entities") or []:
                if not any(e.get("name") == name for e in entities):
                    entities.append({"name": name, "env": env.get("name", "")})
        return [e for e in entities if isinstance(e, dict)]
    entities: List[Dict[str, Any]] = []
    for host in topo.get("hosts") or []:
        if isinstance(host, dict):
            entities.append(dict(host))
    for ch in topo.get("cross_host") or []:
        if isinstance(ch, dict):
            entities.append(dict(ch))
    entities.extend(_all_services(topo, home))
    return entities


def topo_first_layer(topo: Dict[str, Any]) -> Dict[str, Any]:
    """Layer-1 overview content per schema version (for TOPO injection / banner).

    v0.2 returns hosts + cross_host (the system-prompt overview — services
    stay out of the injected block so token cost stays constant as the
    platform grows); v0.1 returns the flat core_entities (compat path).
    """
    if _is_v2_or_v3(topo):
        return {
            "environments": topo.get("environments") or [],
            "clusters": [e for e in topo.get("clusters") or [] if isinstance(e, dict)],
            "hosts": [e for e in topo.get("hosts") or [] if isinstance(e, dict)],
            "cross_host": [e for e in topo.get("cross_host") or [] if isinstance(e, dict)],
            "key_paths": topo.get("key_paths") or [],
        }
    return {
        "environments": topo.get("environments") or [],
        "core_entities": _all_core_entities(topo),
        "key_paths": topo.get("key_paths") or [],
    }


def topology_entity_count(home: Optional[Path] = None) -> int:
    """Total entity count in the flat view (banner display, OPS-DELTA #6)."""
    home = home or _hermes_home()
    topo = load_topology(home)
    if topo is None:
        return 0
    return len(_all_core_entities(topo, home))


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
        data.setdefault("cluster", str(entity.get("cluster") or "default"))
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
    host: Optional[str] = None,
    cluster: Optional[str] = None,
    detail: bool = False,
    home: Optional[Path] = None,
) -> str:
    """Query the topology table. Returns a JSON string (tool contract).

    OPS-DELTA #6 / #42 查询路径:
      - 无参 → 第一层总览（紧凑：environments + clusters + hosts + cross_host）；
      - ``host=<name>`` → 该 host + 其第二层服务索引；
      - ``entity=<name>`` → 跨层名解析：先第二层服务名，再第一层 host/cross_host
        （host 命中时附带其 services 列表）；
      - ``detail=True`` → 加载第三层完整档案；
      - ``type=/env=/cluster=`` → 扁平视图过滤（紧凑字段 name/type/env/cluster/
        endpoint/stale，OPS-DELTA #30 方案 1：列表视图体积裁剪；cluster 缺省
        显示 "default"）。
    v0.1 数据（core_entities）走兼容路径，视图与现有行为一致。
    """
    home = home or _hermes_home()
    topo = load_topology(home)
    if topo is None:
        return tool_error(
            f"拓扑表不存在或无法解析: {_topology_path(home)}。"
            "运维会话需要先铺拓扑数据：运行 vigil ops-init 生成样例 topology.yaml"
            "（schema v0.2，见 ops-agent-harness.md §2.2；v0.1 数据兼容读取），或手动创建。"
        )

    if host:
        return _query_host(topo, home, host, detail)
    if entity:
        return _query_entity(topo, home, entity, detail)

    entities = _all_core_entities(topo, home)
    if entity_type:
        entities = [e for e in entities if e.get("type") == entity_type]
    if env:
        entities = [e for e in entities if e.get("env") == env]
    if cluster:
        entities = [e for e in entities if str(e.get("cluster") or "default") == cluster]
    if not (entity_type or env or cluster):
        # 无参 → 第一层总览（紧凑）；v0.1 兼容：core_entities 扁平列表。
        return _overview(topo, home)
    rows = [_compact_row(e) for e in entities]
    return json.dumps({"count": len(rows), "entities": rows}, ensure_ascii=False, indent=2)


def _with_cluster(entity: Dict[str, Any], cluster: str) -> Dict[str, Any]:
    """服务行继承所属 host 的 cluster（host 查询返回时补全显示字段）。"""
    entity.setdefault("cluster", cluster)
    return entity


def _credential_ref(entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """凭据引用随行返回（LLM 可见）；port 缺省 22，返回时补齐。"""
    cred = entity.get("credential")
    if not isinstance(cred, dict):
        return None
    cred = dict(cred)
    cred.setdefault("port", 22)
    return cred


def _compact_row(entity: Dict[str, Any]) -> Dict[str, Any]:
    """Compact list-view row (OPS-DELTA #30 方案 1): name/type/env/cluster/endpoint/stale."""
    row = {
        "name": entity.get("name", ""),
        "type": entity.get("type", ""),
        "env": entity.get("env", ""),
        "cluster": str(entity.get("cluster") or "default"),
        "endpoint": entity.get("endpoint"),
        "stale": _stale_flag(entity.get("last_verified")),
    }
    cred = _credential_ref(entity)
    if cred is not None:
        row["credential"] = cred
    return row


def _overview(topo: Dict[str, Any], home: Optional[Path] = None) -> str:
    """First-layer overview (compact). v0.1 → core_entities list; v0.2/v0.3 → hosts+cross_host."""
    first = topo_first_layer(topo)
    if _is_v2_or_v3(topo):
        hosts = [_compact_row(h) for h in first["hosts"]]
        cross = [_compact_row(c) for c in first["cross_host"]]
        return json.dumps(
            {
                "version": int(topo.get("version") or 2),
                "count": len(hosts) + len(cross) + len(_all_services(topo, home)),
                "environments": first["environments"],
                "clusters": first["clusters"],
                "hosts": hosts,
                "cross_host": cross,
                "key_paths": first["key_paths"],
                "note": (
                    "第一层总览（系统提示注入层，紧凑）。"
                    "按 host=<name> 展开第二层服务索引；cluster=<name> 过滤；"
                    "entity=<name> 跨层解析；detail=True 进第三层详情。"
                ),
            },
            ensure_ascii=False, indent=2,
        )
    return json.dumps(
        {
            "version": 1,
            "count": len(first["core_entities"]),
            "environments": first["environments"],
            "core_entities": [_compact_row(e) for e in first["core_entities"]],
            "key_paths": first["key_paths"],
            "note": "v0.1 兼容视图：扁平 core_entities（schema v0.2 的 hosts/cross_host 不可用）。",
        },
        ensure_ascii=False, indent=2,
    )


def _query_host(topo: Dict[str, Any], home: Path, host: str, detail: bool) -> str:
    """Host query: layer-1 host row + its layer-2 services index."""
    if not _is_v2_or_v3(topo):
        return tool_error(
            f"拓扑表是 schema v0.1（扁平 core_entities），无 host 概念；"
            f"请用 entity= 查询或迁移到 v0.2。"
        )
    match = next((h for h in topo.get("hosts") or []
                  if isinstance(h, dict) and h.get("name") == host), None)
    if match is None:
        return tool_error(f"拓扑表中不存在 host: {host}")
    result = dict(match)
    result.setdefault("cluster", "default")
    cred = _credential_ref(result)
    if cred is not None:
        result["credential"] = cred
    result["_env_ref"] = _env_for_entity(topo, match)
    index = _load_host_index(home, match) or {}
    result["services"] = [
        _with_cluster(dict(s), result["cluster"])
        for s in index.get("services") or [] if isinstance(s, dict)
    ]
    result["stale"] = _stale_flag(result.get("last_verified"))
    if detail:
        layer3 = _load_entity_file(home, match)
        result["detail"] = layer3 if layer3 is not None else None
        if layer3 is None:
            result["_detail_missing"] = True
    return json.dumps(_strip_internal(result), ensure_ascii=False, indent=2)


def _query_entity(topo: Dict[str, Any], home: Path, entity: str, detail: bool) -> str:
    """Entity query with cross-layer name resolution (OPS-DELTA #6).

    解析顺序：第二层服务名 → 第一层 host → 第一层 cross_host。
    host 命中时附带其 services 列表；service/cross_host 返回自身行。
    """
    matched_layer: str = ""
    match: Optional[Dict[str, Any]] = None
    if _is_v2_or_v3(topo):
        services = _all_services(topo, home)
        for svc in services:
            if svc.get("name") == entity:
                match, matched_layer = dict(svc), "service"
                break
        if match is None:
            for h in topo.get("hosts") or []:
                if isinstance(h, dict) and h.get("name") == entity:
                    match, matched_layer = dict(h), "host"
                    break
        if match is None:
            for c in topo.get("cross_host") or []:
                if isinstance(c, dict) and c.get("name") == entity:
                    match, matched_layer = dict(c), "cross_host"
                    break
    else:
        match = next((e for e in _all_core_entities(topo) if e.get("name") == entity), None)
        matched_layer = "entity"
    if match is None:
        return tool_error(f"拓扑表中不存在实体: {entity}")

    result = dict(match)
    result.setdefault("cluster", "default")
    cred = _credential_ref(result)
    if cred is not None:
        result["credential"] = cred
    result["_env_ref"] = _env_for_entity(topo, match)
    if matched_layer == "host":
        index = _load_host_index(home, match) or {}
        result["services"] = [
            _with_cluster(dict(s), result["cluster"])
            for s in index.get("services") or [] if isinstance(s, dict)
        ]
    result["stale"] = _stale_flag(result.get("last_verified"))
    if detail:
        layer3 = _load_entity_file(home, match)
        if layer3 is not None:
            result["detail"] = layer3
        else:
            result["detail"] = None
            result["_detail_missing"] = True
    return json.dumps(_strip_internal(result), ensure_ascii=False, indent=2)


def _env_for_entity(topo: Dict[str, Any], entity: Dict[str, Any]) -> Optional[str]:
    for env in topo.get("environments") or []:
        if entity.get("name") in (env.get("core_entities") or []):
            return env.get("name")
    if entity.get("env"):
        return entity.get("env")
    # 服务行未显式标 env 时，经 "服务 → 所属 host → env" 链路解析（OPS-DELTA #6）。
    host_name = entity.get("_host")
    if host_name:
        for host in topo.get("hosts") or []:
            if isinstance(host, dict) and host.get("name") == host_name and host.get("env"):
                return host.get("env")
    return None


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
    from tools.ops_data_home import resolve_ops_data_home
    # 读写同一数据 home：数据只挂在解析出的 home 下，查询与更新始终落同一处
    # （sibling ops profile 回退已删除，不存在数据分裂）。
    home = resolve_ops_data_home(home or _hermes_home(), _TOPO_FILENAME)
    topo = load_topology(home)
    if topo is None:
        return tool_error("拓扑表不存在或无法解析，无法更新。")

    if not isinstance(updates, dict) or not updates:
        return tool_error("updates 必须是非空对象。")

    entities = _all_core_entities(topo, home)
    match = next((e for e in entities if e.get("name") == entity), None)
    if match is None:
        return tool_error(f"拓扑表中不存在实体: {entity}")

    env_name = _entity_env(topo, match)
    target_path = _resolve_entity_detail(home, match)
    if target_path is None:
        return tool_error(f"实体 {entity} 的 detail 路径非法（越界 HERMES_HOME）。")

    # PROD 变更 → 审批确认（钉在执行工具层，LLM 无法绕过）。
    # env 四值枚举 + 老自定义名按档位映射（uat → prod 档，权限语义不放松）。
    from tools.topo_discovery import _env_tier
    if _env_tier(env_name) == "prod":
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

    attrs_expanded = False
    for key, value in updates.items():
        if key == "attrs":
            # OPS-DELTA #33：调用方多包一层 attrs → 显式展开合并进顶层 attrs，
            # 不再静默写入 attrs.attrs 嵌套层（曾导致 20 个实体档案两层嵌套）。
            if not isinstance(value, dict):
                return tool_error(
                    f"updates['attrs'] 必须是对象（收到 {type(value).__name__}）；"
                    "字段键直接传，不需要包 attrs 层。"
                )
            attrs = existing.setdefault("attrs", {})
            if not isinstance(attrs, dict):
                attrs = {}
                existing["attrs"] = attrs
            attrs.update(value)
            attrs_expanded = True
        elif key in _TOPLEVEL_UPDATE_FIELDS:
            existing[key] = value
        else:
            # 非标量值防御：dict/list 等复杂结构不再静默写入 attrs
            # （depends_on/depended_by 是已定义的顶层列表字段，走上一分支）。
            # 防 LLM 传嵌套结构继续污染档案。
            if isinstance(value, (dict, list)):
                return tool_error(
                    f"updates['{key}'] 的值是 {type(value).__name__} 复杂结构，"
                    "topo_update 只接受标量属性（防嵌套结构污染档案）；"
                    "字段键直接传，不需要包 attrs 层。"
                )
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
    if attrs_expanded:
        audit["note"] = "updates 含 attrs 键：字段键直接传，不需要包 attrs 层；已自动展开合并。"
    return json.dumps({"status": "updated", **audit}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# topo_status_sync
# ---------------------------------------------------------------------------

# docker 实际状态 → 拓扑 status 语义映射。status 字段语义不变：只写
# running/stopped 既有值，不引入新枚举（paused/created/dead/removing 均归
# stopped——容器不在可服务状态；restarting 归 running——瞬时过渡仍视为拉起）。
_DOCKER_STATE_TO_STATUS = {
    "running": "running",
    "restarting": "running",
    "exited": "stopped",
    "created": "stopped",
    "dead": "stopped",
    "removing": "stopped",
    "paused": "stopped",
}


def _docker_managed(entity: Dict[str, Any]) -> bool:
    """实体是否可做 docker 状态检查（容器实体：attrs.container / type / runtime 标记）。"""
    attrs = entity.get("attrs") or {}
    return bool(
        attrs.get("container")
        or entity.get("type") == "docker-container"
        or attrs.get("runtime") == "docker"
    )


def _enrich_entity_detail(home: Path, entity: Dict[str, Any]) -> Dict[str, Any]:
    """合并第三层详情档案到实体行：attrs/status/type 存在 L3（topo_update
    写 status 的目标文件），L2 索引行只有 name/type/env/detail 引用。

    L2 行已有字段优先（不覆盖），detail 档案只补齐缺失的 attrs/status/type。
    """
    detail = _load_entity_file(home, entity)
    if not detail:
        return entity
    merged = dict(entity)
    merged_attrs = dict(entity.get("attrs") or {})
    for k, v in (detail.get("attrs") or {}).items():
        merged_attrs.setdefault(k, v)
    merged["attrs"] = merged_attrs
    for key in ("status", "type", "env"):
        if detail.get(key) and not merged.get(key):
            merged[key] = detail[key]
    return merged


def _entity_host_name(entity: Dict[str, Any]) -> str:
    """实体所属 docker 主机名：服务行用 _host 标记，第一层行用自身 name。"""
    host = entity.get("_host")
    return str(host or entity.get("name") or "")


def _container_name_for_entity(entity: Dict[str, Any]) -> str:
    """实体 → docker 容器名：attrs.container 最权威，其次 compose_service，兜底实体名。"""
    attrs = entity.get("attrs") or {}
    name = attrs.get("container") or attrs.get("compose_service") or entity.get("name")
    return str(name or "").strip()


def _declared_status(entity: Dict[str, Any]) -> Optional[str]:
    """实体声明的期望状态：顶层 status 优先，其次发现快照 attrs.state。"""
    status = entity.get("status")
    if status:
        return str(status)
    attrs = entity.get("attrs") or {}
    if attrs.get("state"):
        return str(attrs["state"])
    return None


def _runner_for_host(topo: Dict[str, Any], host_name: str):
    """按拓扑 credential 构建主机 docker 探测 runner（复用发现引擎 runner）。

    主机行 credential 为 ssh_key → SSH runner；本机/localhost/拓扑查无此主机 →
    本地 runner（v0.1 兼容）。凭据值不出现：key_path 只传引用路径，密码类凭据
    由 runner 内部经 askpass 注入。
    """
    if not host_name or host_name.lower() in _LOCAL_HOST_ALIASES:
        return _build_local_runner()
    host_row = next(
        (h for h in topo.get("hosts") or [] if isinstance(h, dict) and h.get("name") == host_name),
        None,
    )
    if host_row is None:
        return _build_local_runner()
    cred = host_row.get("credential") or {}
    if not isinstance(cred, dict):
        cred = {}
    key_path = cred.get("ref") if cred.get("type") == "ssh_key" else None
    return _build_ssh_runner(host_name, user=str(cred.get("user") or "root"), key_path=key_path)


def _probe_host_container_states(runner, host_name: str):
    """探测一台主机的 docker ps -a 容器状态表（复用发现引擎解析，不新写探测）。

    Returns:
      (states, None) —— states: {容器名: docker 原始 State}；
      (None, reason) —— 探测失败（exit!=0 / 异常），reason 用户可读。
    """
    try:
        res = runner("docker ps -a --format '{{json .}}'")
    except Exception as exc:
        return None, f"docker 探测失败：{exc}"
    if not getattr(res, "ok", False):
        detail = (res.stderr or res.stdout or "").strip() or "docker 不可用（exit!=0）"
        return None, f"docker 探测失败：{detail[:200]}"
    states: Dict[str, str] = {}
    for c in _parse_docker_ps(res.stdout):
        name = str(c.get("name") or "").lstrip("/")
        if name:
            states[name] = str(c.get("state") or "")
    return states, None


def topo_status_sync(
    host: Optional[str] = None,
    confirm: bool = False,
    home: Optional[Path] = None,
    runner=None,
) -> str:
    """对比拓扑实体状态与实际容器状态（dry-run / confirm 落盘）。

    - ``host`` 省略 → 检查全部 docker 容器实体；指定 → 只查该主机（或实体名）。
    - ``confirm=False`` → 只报告差异不落盘；``confirm=True`` → 对 action=update
      的实体复用 topo_update 写 status（PROD 实体仍走审批），action=ask 不动。
    - 实际状态可确定且与拓扑不一致 → update；不确定（容器缺失/主机不可达/无
      docker 权限）→ ask，留给用户；实体未声明 status 时避免自动覆盖手动值。
    """
    from tools.ops_data_home import resolve_ops_data_home
    home = resolve_ops_data_home(home or _hermes_home(), _TOPO_FILENAME)
    topo = load_topology(home)
    if topo is None:
        return tool_error("拓扑表不存在或无法解析，无法同步状态。")

    entities = [_enrich_entity_detail(home, e) for e in _all_core_entities(topo, home)]
    if host:
        host_key = str(host).strip()
        entities = [
            e for e in entities
            if _entity_host_name(e) == host_key or e.get("name") == host_key
        ]
    docker_entities = [e for e in entities if _docker_managed(e)]
    if not docker_entities:
        return json.dumps(
            {
                "checked": 0,
                "changed": 0,
                "confirm": bool(confirm),
                "differences": [],
                "needs_user": [],
                "note": (
                    "没有可检查的 docker 容器实体（需要 attrs.container / "
                    "type=docker-container / attrs.runtime=docker）。"
                ),
            },
            ensure_ascii=False, indent=2,
        )

    by_host: Dict[str, List[Dict[str, Any]]] = {}
    for e in docker_entities:
        by_host.setdefault(_entity_host_name(e), []).append(e)

    differences: List[Dict[str, Any]] = []
    needs_user: List[Dict[str, Any]] = []
    for host_name, ents in sorted(by_host.items()):
        host_runner = runner or _runner_for_host(topo, host_name)
        states, probe_err = _probe_host_container_states(host_runner, host_name)
        for e in ents:
            rec = {
                "entity": e.get("name"),
                "env": _entity_env(topo, e),
                "host": host_name or "",
            }
            if probe_err:
                needs_user.append({**rec, "action": "ask", "reason": probe_err})
                continue
            container = _container_name_for_entity(e)
            actual_raw = states.get(container)
            if actual_raw is None:
                needs_user.append(
                    {**rec, "action": "ask",
                     "reason": f"容器 {container or '?'} 未在 docker ps -a 中找到"
                               "（已删除或不在本机）"}
                )
                continue
            actual_mapped = _DOCKER_STATE_TO_STATUS.get(actual_raw, actual_raw)
            expected_raw = _declared_status(e)
            expected_mapped = (
                _DOCKER_STATE_TO_STATUS.get(expected_raw, expected_raw)
                if expected_raw else None
            )
            if expected_mapped is None:
                if actual_mapped == "running":
                    # 未声明 status 且容器实际运行：默认期望一致，不产生差异。
                    continue
                needs_user.append(
                    {**rec, "action": "ask", "expected": "(未声明)", "actual": actual_raw,
                     "reason": "实体未声明 status，避免自动覆盖手动值——"
                               "请确认是否写入 status=stopped"}
                )
                continue
            if expected_mapped == actual_mapped:
                continue
            differences.append(
                {**rec, "expected": expected_raw, "actual": actual_raw,
                 "action": "update", "write_status": actual_mapped}
            )

    changed = 0
    write_errors: List[Dict[str, Any]] = []
    if confirm:
        for d in differences:
            result = topo_update(
                d["entity"],
                {"status": d["write_status"]},
                reason="topo_status_sync 自动同步（批次二十四 §R）",
                home=home,
            )
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = {}
            if parsed.get("status") == "updated":
                changed += 1
                d["written"] = True
            else:
                write_errors.append(
                    {"entity": d["entity"], "error": parsed.get("error") or result[:200]}
                )

    return json.dumps(
        {
            "checked": len(docker_entities),
            "changed": changed,
            "confirm": bool(confirm),
            "differences": differences,
            "needs_user": needs_user,
            "write_errors": write_errors,
            "note": (
                "confirm=false 为 dry-run：只报告差异不落盘。differences 中 "
                "action=update 的实体在 confirm=true 时写 status；action=ask 留给用户确认。"
            ),
        },
        ensure_ascii=False, indent=2,
    )


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


def _topology_data_exists(home: Optional[Path] = None) -> bool:
    """topology.yaml 就位且含核心实体（v0.1 core_entities / v0.2 hosts+cross_host）。"""
    path = _topology_path(home)
    try:
        if not path.is_file():
            return False
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return bool(
            data.get("core_entities")
            or data.get("hosts")
            or data.get("cross_host")
            or data.get("clusters")
        )
    except Exception:
        return False


def check_topo_requirements() -> bool:
    """Topo tools are available by default when topology data exists.

    OPS-DELTA #1：能力门控按数据存在性自动切换（装上即用，不再要求 ops-init
    先行）。config 的 ``ops.topology.enabled`` 保留为覆盖开关：
      - 缺省（无该键）→ 按数据存在性（topology.yaml 就位即可用）；
      - 显式 ``enabled: true`` → 仍要求数据就位（无数据工具只会报错）；
      - 显式 ``enabled: false`` → 始终关闭（向后兼容既有关闭配置）。
    """
    ops = _ops_config()
    if ops.get("topology", {}).get("enabled") is False:
        return False
    return _topology_data_exists()


def _query_handler(args: Dict[str, Any], **kwargs) -> str:
    return topo_query(
        entity=args.get("entity"),
        entity_type=args.get("type"),
        env=args.get("env"),
        host=args.get("host"),
        cluster=args.get("cluster"),
        detail=bool(args.get("detail", False)),
    )


def _update_handler(args: Dict[str, Any], **kwargs) -> str:
    return topo_update(
        entity=args.get("entity", ""),
        updates=args.get("updates") or {},
        reason=args.get("reason", ""),
    )


def _discover_handler(args: Dict[str, Any], **kwargs) -> str:
    """会话内发起拓扑发现：复用 discover_host，只返回片段，不落盘。"""
    host = args.get("host")
    env = args.get("env")
    if not host or not env:
        return tool_error("topo_discover 需要 host 与 env")
    creds: Dict[str, Any] = {"user": args.get("user") or "root"}
    if args.get("key"):
        creds["key_path"] = args.get("key")
    discovery = discover_host(str(host), str(env), creds, cluster=str(args.get("cluster") or ""))
    pending = len(discovery.get("services") or [])
    guide = (
        f"发现完成：{pending} 个实体（全部 needs_review=true）。下一步："
        "1) topo_query 查看待审实体；2) topo_update 逐条确认（修正名称/类型/"
        "endpoint，置 needs_review=false）；3) 全部确认后告知用户，实体进入权威拓扑。"
    )
    return json.dumps({**discovery, "_guide": guide}, ensure_ascii=False, default=str)


def _status_sync_handler(args: Dict[str, Any], **kwargs) -> str:
    return topo_status_sync(
        host=args.get("host"),
        confirm=bool(args.get("confirm", False)),
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

registry.register(
    name="topo_discover",
    toolset="topo",
    schema=_TOPO_DISCOVER_SCHEMA,
    handler=_discover_handler,
    emoji="🛰️",
    max_result_size_chars=60_000,
)

registry.register(
    name="topo_status_sync",
    toolset="topo",
    schema=_TOPO_STATUS_SYNC_SCHEMA,
    handler=_status_sync_handler,
    check_fn=check_topo_requirements,
    emoji="🔁",
    max_result_size_chars=30_000,
)
