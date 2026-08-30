"""Ops Agent Harness — 目标解析层（batch83：矩阵按目标实体 env 裁决）。

设计定案（2026-08-30，yapl-design.md §11.1/§六 topo_ref 三层闭环同源）：
执行请求 → classifier 动作枚举 → **变更类动作强制目标解析**（从命令文本提取
目标拓扑实体）→ 实体 env → 查操作矩阵。解析不出实体 → **拒绝执行（deny）**，
deny 是目标解析层的硬拦截、发生在查矩阵之前；矩阵本身仍无 deny 档。

本模块职责（只读、无副作用）：
- ``resolve_required_target``：高危变更动作的目标解析，返回 resolved 或 failed
  （永不静默取会话 env 兜底）。
- 首批高危动作集 ``HIGH_RISK_MUTATING_ACTIONS``（install/remove/decommission/
  reboot/scale + kubectl delete 类——classifier 归 decommission）。
- ssh/scp/sftp/rsync 受控通道：远端执行目标必须解析（"不裸连直跑"，未登记
  拓扑 → 解析失败）。
- 本机命令目标 = 拓扑表里本机 host 实体（按主机名/网卡 IP 匹配）；本机未登记
  拓扑 = 解析失败（无"本机默认 dev"兜底）。

解析源（命令文本 → 拓扑实体）：
- ssh/scp/sftp/rsync 的 user@host / 裸 IP → host 实体（主机名/别名/IP 匹配）。
- kubectl --context/--namespace/--kubeconfig → cluster/service 实体；未指定且
  唯一集群 → 该集群；多集群无法确定 → 失败。
- docker -H/--host/--context → host 实体；本地 socket/无目标 → 本机 host。
- ansible -i <inventory> 主机组 → 实体集合（集合内 env 一致 → 该 env；跨环境
  → 失败；含拓扑表外主机 → 失败）。
- 无目标参数 → 本机 host 实体（拓扑表匹配主机名/网卡 IP）。
"""

from __future__ import annotations

import logging
import re
import shlex
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TARGET_RESOLVED = "resolved"
TARGET_FAILED = "failed"

# 首批高危变更动作（设计定案 §4）：install/remove/decommission/reboot/scale +
# kubectl delete 类（classifier 归 decommission）。低危变更（start/stop/restart/
# reload/upgrade 等）首批暂保持会话 env 裁决（过渡态，dogfood 后定收紧档位）。
HIGH_RISK_MUTATING_ACTIONS: frozenset = frozenset({
    "install", "remove", "decommission", "reboot", "scale",
})

_SSH_FAMILY_RE = re.compile(r"(?<!\w)(?:ssh|scp|sftp|rsync)\b", re.IGNORECASE)
_KUBECTL_RE = re.compile(r"(?<!\w)kubectl\b", re.IGNORECASE)
_DOCKER_RE = re.compile(r"(?<!\w)docker\b", re.IGNORECASE)
_DOCKER_CONTEXT_RE = re.compile(
    r"docker\s+context\s+(?:use|create|update)\s+(\S+)", re.IGNORECASE
)
_K8S_TYPES = frozenset({"k8s", "k3s", "kind"})
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _failed(reason: str) -> Dict[str, Any]:
    """解析失败态：调用方（任务 3）走 deny（先于矩阵，硬拦截）。"""
    return {"status": TARGET_FAILED, "reason": reason}


def _resolved(topo: Dict[str, Any], entity: Dict[str, Any],
              matched_by: str) -> Dict[str, Any]:
    """解析成功态：实体 env 从拓扑表读（环境定义列表 → 实体 env → 服务继承链）。"""
    try:
        from tools.topo_tools import _env_for_entity
        env = _env_for_entity(topo, entity) or entity.get("env") or ""
    except Exception:
        env = str(entity.get("env") or "")
    env = str(env).strip().lower()
    name = str(entity.get("name") or "")
    if not env:
        return _failed(
            f"实体 {name or '(未命名)'} 无 env（拓扑数据缺口）——先 topo_update 补 env 字段"
        )
    return {
        "status": TARGET_RESOLVED,
        "entity": name,
        "env": env,
        "label": f"{name} ({env})",
        "matched_by": matched_by,
    }


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _local_host_identities() -> List[str]:
    """本机身份：主机名 + 全限定名 + 网卡 IP（小写、去尾点、去重）。

    测试可 monkeypatch 本函数（或传 ``local_identities`` 参数）注入本机身份。
    """
    ids: List[str] = []
    try:
        hn = socket.gethostname()
        if hn:
            ids.append(hn)
    except Exception:
        pass
    try:
        fqdn = socket.getfqdn()
        if fqdn and (not ids or fqdn.lower() != ids[0]):
            ids.append(fqdn)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr and ":" not in addr and not addr.startswith("127."):
                ids.append(addr)
    except Exception:
        pass
    seen: set = set()
    out: List[str] = []
    for raw in ids:
        ident = str(raw).strip().lower().rstrip(".")
        if ident and ident not in seen:
            seen.add(ident)
            out.append(ident)
    return out


def _flag_value(command: str, flags: Tuple[str, ...]) -> Optional[str]:
    """命令里取 ``--flag value`` / ``--flag=value`` / ``-n value`` 的值。"""
    try:
        tokens = shlex.split(command)
    except Exception:
        return None
    for i, tok in enumerate(tokens):
        for flag in flags:
            if tok == flag:
                if i + 1 < len(tokens):
                    return tokens[i + 1]
                return None
            if tok.startswith(f"{flag}="):
                return tok[len(flag) + 1:] or None
        if not tok.startswith("--") and tok.startswith("-") and len(tok) > 2:
            short = tok[:2]
            if short in flags:
                return tok[2:] or None
    return None


def _resolve_local(topo: Dict[str, Any], entities: List[Dict[str, Any]],
                   local_identities: Optional[List[str]]) -> Dict[str, Any]:
    """无目标参数 → 本机 host 实体；本机未登记拓扑 = 解析失败（不借会话 env）。"""
    from tools.ops_target import _match_entity
    raw = local_identities if local_identities is not None else _local_host_identities()
    seen: set = set()
    uniq: List[str] = []
    for ident in raw:
        i = str(ident).strip().lower().rstrip(".")
        if i and i not in seen:
            seen.add(i)
            uniq.append(i)
    for ident in uniq:
        entity = _match_entity(entities, ident)
        if entity is not None:
            return _resolved(topo, entity, f"本机 {ident}")
    shown = uniq or ["（无法获取本机身份）"]
    return _failed(
        f"本机未在拓扑表登记（本机身份: {', '.join(shown)}）——高危变更目标解析失败。"
        "修复指引：先 topo_query 查本机实体名，或用 topo_update / topo-discover "
        "把本机 host 登记进拓扑表后再执行。"
    )


def _resolve_kubectl(topo: Dict[str, Any], entities: List[Dict[str, Any]],
                     command: str, home: Path) -> Dict[str, Any]:
    """kubectl → cluster/service 实体：--context/--namespace/--kubeconfig 收敛。"""
    from tools.ops_target import _match_entity  # noqa: F401（_resolved 复用同源匹配）
    clusters = [dict(c) for c in (topo.get("clusters") or [])
                if isinstance(c, dict) and c.get("name")]
    if not clusters:
        clusters = [dict(e) for e in entities
                    if str(e.get("type") or "").lower() in _K8S_TYPES]

    def _by_name(needle: str) -> Optional[Dict[str, Any]]:
        needle = str(needle).strip().lower()
        for c in clusters:
            if str(c.get("name") or "").lower() == needle \
                    or str(c.get("context") or "").lower() == needle:
                return c
        return None

    ctx = _flag_value(command, ("--context", "--kube-context"))
    if ctx:
        cluster = _by_name(ctx)
        if cluster is not None:
            return _resolved(topo, cluster, f"kubectl --context {ctx}")
        return _failed(
            f"kubectl 上下文 {ctx!r} 未匹配拓扑集群/实体——修复指引：先 topo_query "
            "查集群名（或集群行的 context 字段），核对 --context 拼写。"
        )
    ns = _flag_value(command, ("--namespace", "-n"))
    if ns:
        for c in clusters:
            namespaces = c.get("namespaces") or []
            if isinstance(namespaces, list) \
                    and ns in {str(n) for n in namespaces}:
                return _resolved(topo, c, f"kubectl --namespace {ns}")
        if len(clusters) == 1:
            return _resolved(topo, clusters[0], f"kubectl --namespace {ns}（单集群）")
        return _failed(
            f"kubectl 命名空间 {ns!r} 未匹配拓扑集群（且集群数 > 1，无法唯一确定）"
            "——先 topo_query 查集群名，或用 --context 显式指定。"
        )
    kubeconfig = _flag_value(command, ("--kubeconfig",))
    if kubeconfig:
        for c in clusters:
            if str(c.get("kubeconfig") or "") == kubeconfig:
                return _resolved(topo, c, "kubectl --kubeconfig")
        if len(clusters) == 1:
            return _resolved(topo, clusters[0], "kubectl --kubeconfig（单集群）")
        return _failed(
            f"kubectl --kubeconfig {kubeconfig!r} 未匹配拓扑集群（且集群数 > 1）"
            "——先 topo_query 查集群名。"
        )
    if len(clusters) == 1:
        return _resolved(topo, clusters[0], "kubectl（唯一集群）")
    return _failed(
        "kubectl 未指定 --context/--namespace/--kubeconfig 且拓扑表有多个集群，"
        "无法确定目标——先 topo_query 查集群名，或显式 --context 指定。"
    )


def _docker_host_value(command: str) -> Optional[str]:
    """docker -H/--host 的值（-H host / --host host / --host=host / -Hhost）。"""
    try:
        tokens = shlex.split(command)
    except Exception:
        return None
    for i, tok in enumerate(tokens):
        if tok in ("-H", "--host"):
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.startswith("--host="):
            return tok[len("--host="):] or None
        if tok.startswith("-H") and not tok.startswith("--") and len(tok) > 2:
            return tok[2:] or None
    return None


def _resolve_docker(topo: Dict[str, Any], entities: List[Dict[str, Any]],
                    command: str, home: Path,
                    local_identities: Optional[List[str]]) -> Dict[str, Any]:
    """docker -H/--context/context → host 实体；本地 socket/无目标 → 本机 host。"""
    m = _DOCKER_CONTEXT_RE.search(command)
    ctx = _flag_value(command, ("--context",)) or (m.group(1) if m else None)
    if ctx:
        for e in entities:
            if str(e.get("name")) == ctx:
                return _resolved(topo, e, f"docker context {ctx}")
        return _failed(
            f"docker context/--context {ctx!r} 未匹配拓扑实体——先 topo_query 查实体名。"
        )
    host_val = _docker_host_value(command)
    if host_val:
        stripped = str(host_val).strip()
        if stripped.lower().startswith("unix://"):
            return _resolve_local(topo, entities, local_identities)
        for scheme in ("tcp://", "ssh://", "http://", "https://"):
            if stripped.lower().startswith(scheme):
                stripped = stripped[len(scheme):]
                break
        stripped = stripped.split("/", 1)[0].split(":", 1)[0].strip().lower()
        if not stripped:
            return _failed(f"docker -H 目标无法解析: {host_val!r}")
        from tools.ops_target import _match_entity
        entity = _match_entity(entities, stripped)
        if entity is not None:
            return _resolved(topo, entity, f"docker -H {host_val}")
        return _failed(
            f"docker 目标主机 {stripped!r} 未在拓扑表登记——先 topo_query 查实体名"
            "（主机名/别名/IP），或补拓扑登记。"
        )
    return _resolve_local(topo, entities, local_identities)


def _resolve_ansible(topo: Dict[str, Any], entities: List[Dict[str, Any]],
                     command: str, home: Path) -> Dict[str, Any]:
    """ansible -i <inventory> 主机组 → 实体集合 env（集合一致才裁决）。"""
    from tools.ansible_inventory_guard import (
        _extract_inventory_arg,
        _resolve_inventory_path,
        parse_inventory,
    )
    from tools.ops_target import _match_entity
    inventory = _extract_inventory_arg(command)
    if not inventory:
        return _failed("ansible 未指定 -i inventory，无法解析目标主机集合。")
    path = _resolve_inventory_path(inventory, home)
    if path is None or not path.is_file():
        return _failed(
            f"ansible inventory 文件读不到: {inventory!r}（fail-closed）——确认 "
            "inventory 路径真实存在，或改用拓扑表主机。"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return _failed(f"ansible inventory 读取失败: {path}（{exc}）")
    hosts = parse_inventory(text)
    if not hosts:
        return _failed(
            f"ansible inventory 格式无法识别（ini/yaml 均未解析出主机）: {path}"
        )
    resolved: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    for host in sorted(hosts):
        entity = _match_entity(entities, host)
        if entity is not None:
            resolved.append(entity)
        else:
            unresolved.append(host)
    if unresolved:
        return _failed(
            f"ansible inventory 含拓扑表外主机: {unresolved}——inventory 是拓扑表的"
            "派生物（yapl-design.md §六）；先 topo_query 查实体名或补拓扑登记。"
        )
    envs = {str(_resolved_env(topo, e)) for e in resolved}
    envs.discard("")
    if not envs:
        return _failed("ansible 主机集合全部缺 env（拓扑数据缺口）——先 topo_update 补 env。")
    if len(envs) == 1:
        env = envs.pop()
        return {
            "status": TARGET_RESOLVED,
            "entity": f"ansible-group({len(resolved)})",
            "env": env,
            "label": f"ansible 主机组 ×{len(resolved)} ({env})",
            "matched_by": f"ansible -i {path.name}",
        }
    return _failed(
        f"ansible 主机集合跨环境（{sorted(envs)}），无法按单行矩阵裁决——请拆分"
        "主机组或先 topo_update 对齐 env。"
    )


def _resolved_env(topo: Dict[str, Any], entity: Dict[str, Any]) -> str:
    try:
        from tools.topo_tools import _env_for_entity
        return str(_env_for_entity(topo, entity) or entity.get("env") or "").strip().lower()
    except Exception:
        return str(entity.get("env") or "").strip().lower()


def command_target_required(actions: set, command: str) -> bool:
    """高危变更动作 / ssh 族受控通道 → 需要强制目标解析。

    - 动作命中 ``HIGH_RISK_MUTATING_ACTIONS`` → True。
    - ssh/scp/sftp/rsync（classifier 归 run_script/transfer_file，动作不在高危
      集）→ True：受控通道"不裸连直跑"，远端目标必须解析，未登记 → deny。
    - 只读动作（query/fetch_log/verify）→ False（天然无目标实体，不误伤）。
    """
    if actions & HIGH_RISK_MUTATING_ACTIONS:
        return True
    if not isinstance(command, str):
        return False
    return bool(_SSH_FAMILY_RE.search(command))


def resolve_required_target(command: str, *,
                            local_identities: Optional[List[str]] = None) -> Dict[str, Any]:
    """高危变更动作目标解析：resolved 或 failed（永不静默取会话 env 兜底）。

    Args:
        command: 原始命令串。
        local_identities: 本机身份覆盖（测试注入用）；None → 运行时探测
            （hostname/fqdn/网卡 IP）。

    Returns:
        resolved: {"status": "resolved", "entity", "env", "label", "matched_by"}。
        failed:   {"status": "failed", "reason"}——调用方（任务 3）走 deny。
    """
    if not isinstance(command, str) or not command.strip():
        return _failed("命令为空，无法解析目标。")
    try:
        from tools.topo_tools import _all_core_entities, load_topology
    except Exception as exc:
        return _failed(f"拓扑库不可用: {exc}")
    home = _hermes_home()
    topo = load_topology(home)
    if not topo:
        return _failed(
            "拓扑表不可用（topology.yaml 缺失/解析失败）——修复指引：先运行 "
            "vigil ops-init 铺拓扑数据，或 topo_query 确认拓扑表可读。"
        )
    entities = _all_core_entities(topo, home)
    if not entities:
        return _failed(
            "拓扑表为空（无任何实体）——先 topo_query / topo_update 登记实体后再执行。"
        )

    from tools.ops_target import _host_candidates, _match_entity

    hosts = _host_candidates(command)
    if hosts:
        for host in hosts:
            entity = _match_entity(entities, host)
            if entity is not None:
                return _resolved(topo, entity, f"ssh 目标 {host}")
        return _failed(
            f"目标主机未在拓扑表登记: {hosts}——修复指引：先 topo_query 查实体名"
            "（主机名/别名/IP），或用 topo_update 登记后再执行。"
        )
    if _KUBECTL_RE.search(command):
        return _resolve_kubectl(topo, entities, command, home)
    if _DOCKER_RE.search(command):
        return _resolve_docker(topo, entities, command, home, local_identities)
    from tools.ansible_inventory_guard import _extract_inventory_arg, _is_ansible_command
    if _is_ansible_command(command) and _extract_inventory_arg(command):
        return _resolve_ansible(topo, entities, command, home)
    return _resolve_local(topo, entities, local_identities)


__all__ = [
    "TARGET_RESOLVED",
    "TARGET_FAILED",
    "HIGH_RISK_MUTATING_ACTIONS",
    "command_target_required",
    "resolve_required_target",
    "_local_host_identities",
]
