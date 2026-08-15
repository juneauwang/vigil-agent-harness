"""Ops Agent Harness — command target resolution（跨环境硬约束 · 命令目标级 env 判定）.

从命令字符串解析目标主机（ssh/scp/sftp 的 ``user@host``、裸 ``@<ip>``），
再把主机映射到拓扑实体（topology.yaml 的 ``name`` / ``endpoint`` / 第二层
``attrs.public_ip`` / ``attrs.internal_ip``），得到目标级 env；含 ``kubectl``
的命令关联 ``k3s-prod`` 集群实体（env=prod）。调用方（tools/approval.py）拿到
目标 env 后按矩阵判定，把"跨环境操作默认拒绝"从行为约束升级为硬 gate。

边界（防误伤，ops-agent-harness.md §3）:
- 解析不到目标 / 拓扑查无此实体 → 返回 None，调用方完全走现状（会话 env）。
- 本模块只读、无副作用；``ops.permissions.enabled=false`` 时调用方不查。
- 命令里带 IP 但拓扑查无此实体（临时机器）→ None，不 fail-closed 误伤。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SSH_CMD_RE = re.compile(r"(?<!\w)(?:ssh|scp|sftp)\b", re.IGNORECASE)
# user@host —— host 允许冒号（scp 的 host:path 形式），取第一段。
_USER_AT_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9._-])([A-Za-z0-9._-]+)@([A-Za-z0-9][A-Za-z0-9._:-]*)"
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_KUBECTL_RE = re.compile(r"(?<!\w)kubectl\b", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _host_candidates(command: str) -> List[str]:
    """Extract candidate target hosts in command order, de-duplicated.

    ssh/scp/sftp 命令：user@host 全部保留（含 hostname 与 IP）；非 ssh 命令
    只保留 ``user@<ipv4>`` 形式的（避免 git@github.com:org/repo 之类误命中）。
    """
    lowered = command.lower()
    is_ssh_family = bool(_SSH_CMD_RE.search(lowered))
    hosts: List[str] = []
    for m in _USER_AT_HOST_RE.finditer(command):
        host = m.group(2).split(":", 1)[0] if is_ssh_family else m.group(2)
        host = host.rstrip(".")
        if host and (is_ssh_family or _IPV4_RE.fullmatch(host)) and host not in hosts:
            hosts.append(host)
    # ssh 裸 IP（无 user 前缀，如 "ssh -i key 203.0.113.11"）
    if is_ssh_family:
        for m in _IPV4_RE.finditer(command):
            host = m.group(0)
            if host not in hosts:
                hosts.append(host)
    return hosts


def _endpoint_raw(endpoint: Any) -> Optional[str]:
    """Strip scheme and trailing slash from an endpoint, keeping the port."""
    if not endpoint:
        return None
    host = str(endpoint).strip()
    host = _SCHEME_RE.sub("", host)
    host = host.rstrip("/")
    return host.lower() or None


def _endpoint_host(endpoint: Any) -> Optional[str]:
    """Strip scheme/port from an endpoint value → bare host (or None)."""
    host = _endpoint_raw(endpoint)
    if not host:
        return None
    return host.split(":", 1)[0] or None


def _entity_ids(entity: Dict[str, Any], attrs: Optional[Dict[str, Any]]) -> List[str]:
    """All identifiers a host string may match for one entity (lowercased)."""
    ids: List[str] = []
    name = entity.get("name")
    if name:
        ids.append(str(name).lower())
    ep_host = _endpoint_host(entity.get("endpoint"))
    if ep_host:
        ids.append(ep_host)
    attrs_map = (attrs or {}).get("attrs") if isinstance(attrs, dict) else None
    for key in ("public_ip", "internal_ip"):
        val = (attrs_map or {}).get(key)
        if val:
            ids.append(str(val).strip().lower())
    return ids


def _exact_ids(entity: Dict[str, Any], attrs: Optional[Dict[str, Any]]) -> List[str]:
    """Identifiers compared by full equality — the raw endpoint keeps its port.

    ``node1.endpoint == "203.0.113.10"`` is an exact endpoint for the bare
    host needle, while ``harbor.endpoint == "203.0.113.10:30443"`` is not —
    the port makes it a different string, so it only ever matches in the
    contains fallback pass.
    """
    ids: List[str] = []
    name = entity.get("name")
    if name:
        ids.append(str(name).lower())
    raw_ep = _endpoint_raw(entity.get("endpoint"))
    if raw_ep:
        ids.append(raw_ep)
    attrs_map = (attrs or {}).get("attrs") if isinstance(attrs, dict) else None
    for key in ("public_ip", "internal_ip"):
        val = (attrs_map or {}).get(key)
        if val:
            ids.append(str(val).strip().lower())
    return ids


def _match_entity(entities: List[Dict[str, Any]], host: str) -> Optional[Dict[str, Any]]:
    """Return the topology entity matching ``host``, exact equality first.

    Exact-equality identifiers win over substring matches: a bare IP that is
    one entity's full endpoint (e.g. ``node1.endpoint == "203.0.113.10"``)
    must not be captured by an earlier entity whose endpoint merely contains
    it (e.g. ``harbor.endpoint == "203.0.113.10:30443"``). Substring matching
    over the port-stripped identifiers stays as the fallback so ported
    service endpoints and partial hostnames still resolve.
    """
    needle = host.strip().lower()
    if not needle:
        return None
    try:
        from tools.topo_tools import _load_entity_file
    except Exception as exc:  # pragma: no cover - import path guarded
        logger.debug("ops_target: topo_tools unavailable: %s", exc)
        return None
    home = _hermes_home()

    def _loaded(entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        attrs = None
        try:
            attrs = _load_entity_file(home, entity)
        except Exception as exc:
            logger.debug("ops_target: entity detail load failed for %s: %s",
                         entity.get("name"), exc)
        return attrs

    loaded = [_loaded(entity) for entity in entities]
    for entity, attrs in zip(entities, loaded):
        if needle in _exact_ids(entity, attrs):
            return entity
    for entity, attrs in zip(entities, loaded):
        if needle in _entity_ids(entity, attrs):
            return entity
    return None


def _build_target(topo: Dict[str, Any], entity: Dict[str, Any],
                  matched_by: str) -> Optional[Dict[str, Any]]:
    """Resolve the entity's env from the topology environments list."""
    try:
        from tools.topo_tools import _env_for_entity
        env = _env_for_entity(topo, entity) or entity.get("env") or ""
    except Exception as exc:  # pragma: no cover - import path guarded
        logger.debug("ops_target: env resolution failed: %s", exc)
        return None
    env = str(env).strip().lower()
    if not env:
        # 实体无 env（数据缺口）→ 无法做目标级判定，交回现状。
        return None
    name = str(entity.get("name") or "")
    return {
        "entity": name,
        "env": env,
        "label": f"{name} ({env})",
        "matched_by": matched_by,
    }


def resolve_command_target(command: str) -> Optional[Dict[str, Any]]:
    """Resolve a terminal command to a topology target entity.

    Returns None when the command has no target, the target is not in the
    topology table, or the topology is unavailable — callers then keep the
    existing session-env behavior unchanged.

    Returned dict: ``{"entity", "env", "label", "matched_by"}`` where
    ``label`` is the human-facing "node2 (prod)" form.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        from tools.topo_tools import _all_core_entities, load_topology
    except Exception as exc:
        logger.debug("ops_target: topo_tools unavailable: %s", exc)
        return None
    home = _hermes_home()
    topo = load_topology(home)
    if not topo:
        return None
    # OPS-DELTA #6：v0.2 下扁平视图含第二层服务（服务经 "服务 → 所属 host →
    # env" 链路继承 env）——ssh/scp 命中服务实体时同样能解析目标级 env。
    entities = _all_core_entities(topo, home)
    if not entities:
        return None

    for host in _host_candidates(command):
        entity = _match_entity(entities, host)
        if entity is not None:
            return _build_target(topo, entity, "host")

    if _KUBECTL_RE.search(command):
        for entity in entities:
            if entity.get("name") == "k3s-prod":
                return _build_target(topo, entity, "kubectl")

    return None
