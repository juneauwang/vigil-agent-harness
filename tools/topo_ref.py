"""YAPL 主框架阶段 A：``resolve_topo_ref`` 共享库（topo_ref 引用解析）。

设计依据：yapl-design.md 第十三章 §13.3。值 = 裸实体名（不带集群），kind 由
契约声明限定。解析顺序（**永不静默取第一个**）：

1. 精确匹配：kind 限定（若有）下名字唯一 → 返回；
2. 上下文收敛：重名时用调用上下文（``{env?, cluster?, host?}``——runbook
   执行范围 / 调用上下文）过滤 → 范围内唯一 → 返回；
3. 歧义报错：仍多个 → 报错列候选（实体全名 ``cluster__host__name`` +
   env/cluster 归属）→ 引导 clarify 或改用全名；
4. 无匹配拒绝：不存在 → 拒绝 + 提示重查拓扑（先 ``topo_query`` 确认实体名）。

实体获取与 P4 ``resolve_target`` 同源（``tools.topo_tools._all_services`` /
topo 行），阶段 B（生成工具）与 P4 执行器共用同一接口。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


_KINDS = ("service", "host", "cluster", "host_group")


def _candidates(topo: Dict[str, Any],
                home=None) -> List[Tuple[Dict[str, Any], str]]:
    """全部 topo_ref 候选：[(实体行, kind)]。

    cluster / host_group / host 来自 topo 第一层行；service 来自
    ``_all_services``（需要 ``home`` 读 services 索引）；v0.2/3 存量
    ``cross_host`` 行并入 host 层（与 P4 ``resolve_target`` 旧行为一致）。
    """
    out: List[Tuple[Dict[str, Any], str]] = []
    for c in topo.get("clusters") or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        out.append((c, "cluster"))
        for hg in c.get("host_groups") or []:
            row = hg if isinstance(hg, dict) else {"name": hg}
            if not row.get("name"):
                continue
            row = dict(row)
            row.setdefault("env", c.get("env") or "")
            row.setdefault("cluster", c.get("name"))
            out.append((row, "host_group"))
    for h in topo.get("hosts") or []:
        if isinstance(h, dict) and h.get("name"):
            out.append((h, "host"))
    for ch in topo.get("cross_host") or []:
        if isinstance(ch, dict) and ch.get("name"):
            out.append((ch, "host"))
    if home is not None:
        try:
            from tools.topo_tools import _all_services
            for s in _all_services(topo, home):
                if isinstance(s, dict) and s.get("name"):
                    out.append((s, "service"))
        except Exception:
            pass
    return out


def _entity_full_name(kind: str, e: Dict[str, Any]) -> str:
    """实体全名 ``cluster__host__name``（歧义候选展示用）。"""
    name = str(e.get("name") or "")
    cluster = str(e.get("cluster") or e.get("env") or "")
    if kind == "service":
        return f"{cluster}__{e.get('_host') or ''}__{name}"
    if kind == "host":
        return f"{cluster}__{name}__{name}"
    if kind == "cluster":
        return f"{name}____{name}"
    if kind == "host_group":
        return f"{cluster}____{name}"
    return f"{cluster}__{e.get('_host') or ''}__{name}"


def _entity_host(kind: str, e: Dict[str, Any]) -> str:
    """实体归属 host：service 用 ``_host`` 标记，host 层用自身 name。"""
    if kind == "host":
        return str(e.get("name") or "")
    return str(e.get("_host") or "")


def _in_scope(e: Dict[str, Any], kind: str,
              context: Optional[Dict[str, Any]]) -> bool:
    """上下文过滤：``{env?, cluster?, host?}`` 均带值才参与比对；实体侧
    缺该字段（未知）不排除——未知不参与收敛，避免误杀。"""
    ctx = context or {}
    if ctx.get("env"):
        ent_env = str(e.get("env") or "")
        if ent_env and ent_env != str(ctx["env"]):
            return False
    if ctx.get("cluster"):
        ent_cluster = str(e.get("cluster") or "")
        if ent_cluster and ent_cluster != str(ctx["cluster"]):
            return False
    if ctx.get("host"):
        ent_host = _entity_host(kind, e)
        if ent_host and ent_host != str(ctx["host"]):
            return False
    return True


def resolve_topo_ref(topo: Dict[str, Any], name: str,
                     kind: Optional[str] = None,
                     context: Optional[Dict[str, Any]] = None,
                     home=None) -> Dict[str, Any]:
    """topo_ref 引用解析（永不静默取第一个）。

    Args:
        topo: 拓扑 dict（``tools.topo_tools.load_topology`` 产物）。
        name: 裸实体名（不带集群）。
        kind: 限定拓扑层（service/host/cluster/host_group）；None = 任意层。
        context: 调用上下文范围 ``{env?, cluster?, host?}``（runbook 执行
            范围 / 调用上下文）；None = 无范围。
        home: VIGIL_HOME（service 层读 services 索引需要；None = 只查第一层）。

    Returns:
        命中的实体行 dict（拷贝，附 ``_kind`` 归属标记；不改动拓扑原行）。

    Raises:
        ValueError: 无匹配（提示先 topo_query）或重名歧义（列候选 + 引导）。
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("topo_ref 值必填——裸实体名，不带集群")
    pairs = [(e, k) for e, k in _candidates(topo, home)
             if (kind is None or k == kind) and str(e.get("name")) == name]
    if not pairs:
        raise ValueError(
            f"topo_ref {name!r} 不在拓扑表（kind={kind or '任意'}）——"
            "target 是拓扑实体引用（service/host/host_group/cluster），"
            "先 topo_query 确认实体名"
        )
    if len(pairs) == 1:
        return _with_kind(pairs[0][0], pairs[0][1])
    scoped = [(e, k) for e, k in pairs if _in_scope(e, k, context)]
    if len(scoped) == 1:
        return _with_kind(scoped[0][0], scoped[0][1])
    report = scoped if scoped else pairs
    listing = "；".join(
        f"{_entity_full_name(k, e)}"
        f"（env={e.get('env') or '-'}, cluster={e.get('cluster') or '-'}）"
        for e, k in report
    )
    raise ValueError(
        f"topo_ref {name!r} 重名歧义（{len(report)} 个候选）：{listing}——"
        "不静默取第一个；请用调用上下文收敛（env/cluster/host）或改用"
        "实体全名 cluster__host__name"
    )


def _with_kind(e: Dict[str, Any], kind: str) -> Dict[str, Any]:
    row = dict(e)
    row["_kind"] = kind
    return row


def entity_full_name_of(e: Dict[str, Any]) -> str:
    """已解析实体（含 ``_kind``）的全名展示辅助。"""
    return _entity_full_name(str(e.get("_kind") or ""), e)


__all__ = [
    "_KINDS",
    "_candidates",
    "_entity_full_name",
    "_in_scope",
    "resolve_topo_ref",
    "entity_full_name_of",
]
