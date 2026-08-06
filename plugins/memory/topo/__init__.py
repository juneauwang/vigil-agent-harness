"""Topo — 平台拓扑表（事实层）MemoryProvider 插件（Ops Agent Harness）.

零侵入注入：system prompt 的 external memory provider block 是现成注入槽位，
本 provider 把 topology.yaml 第一层总览渲染成 TOPO 段放进去（ops-agent-harness.md
§7 A 方案）。平台事实不进 ~2KB 内置 memory；拓扑工具（topo_query/topo_update）
走 registry 的 ``topo`` toolset（tools/topo_tools.py），不通过本 provider 暴露，
避免双注册。

激活方式（ops profile 的 config.yaml）:
    memory:
      provider: topo
    ops:
      topology:
        enabled: true
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

# 行为约束（ops-agent-harness.md §4 C）随 TOPO 段注入，不碰核心 tool_guidance。
_BEHAVIOR_CONSTRAINT = (
    "执行任何运维操作前，先 topo_query 确认目标实体在拓扑表中的身份和环境；"
    "跨环境操作默认拒绝。"
)


def _load_ops_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}) or {}
    except Exception:
        return {}


def _load_topology_l1(home: Path):
    """Return (topo_dict, topology_path); (None, path) when absent/broken."""
    path = home / "topology.yaml"
    if not path.is_file():
        return None, path
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("topo provider: failed to parse %s: %s", path, exc)
        return None, path
    if not isinstance(data, dict):
        return None, path
    return data, path


def _env_line(env: Dict[str, Any]) -> str:
    parts = [str(env.get("name", "?")), f"isolation={env.get('isolation', '?')}",
             f"role={env.get('role', '?')}"]
    entry = env.get("entry")
    if entry:
        parts.append(f"entry={entry}")
    return "- " + ", ".join(parts)


def _entity_line(entity: Dict[str, Any]) -> str:
    bits = [str(entity.get("name", "?")), f"type={entity.get('type', '?')}",
            f"env={entity.get('env', '?')}"]
    if entity.get("endpoint"):
        bits.append(f"endpoint={entity.get('endpoint')}")
    owner = entity.get("owner")
    if owner:
        bits.append(f"owner={owner}")
    source = entity.get("source")
    if source:
        bits.append(f"source={source}")
    return f"- {', '.join(bits)}"


def render_topo_block(home: Path, max_lines: int = 45) -> str:
    """Render the layer-1 topology overview as the TOPO system-prompt section.

    Returns "" when the topology table is missing/disabled so the injection
    slot stays silent (no prompt-cache churn for non-ops profiles).
    """
    topo, path = _load_topology_l1(home)
    if topo is None:
        return ""

    lines: List[str] = ["## TOPO — 平台拓扑总览（事实层，第一层）"]

    envs = topo.get("environments") or []
    if envs:
        lines.append("环境:")
        lines.extend(_env_line(e) for e in envs if isinstance(e, dict))

    entities = list(topo.get("core_entities") or [])
    for env in envs:
        if not isinstance(env, dict):
            continue
        for name in env.get("core_entities") or []:
            if not any(e.get("name") == name for e in entities):
                entities.append({"name": name, "env": env.get("name", "")})
    if entities:
        lines.append("核心实体:")
        lines.extend(_entity_line(e) for e in entities if isinstance(e, dict))

    key_paths = topo.get("key_paths") or []
    if key_paths:
        lines.append("关键链路（排障优先）:")
        for kp in key_paths:
            if isinstance(kp, list):
                lines.append("- " + " → ".join(str(x) for x in kp))

    lines.append("")
    lines.append(_BEHAVIOR_CONSTRAINT)

    block = "\n".join(lines)
    if len(lines) > max_lines:
        block = "\n".join(lines[: max_lines - 1]) + "\n- ... (truncated)"
    return block


class TopoMemoryProvider(MemoryProvider):
    """Injects the layer-1 topology overview into the system prompt."""

    @property
    def name(self) -> str:
        return "topo"

    def is_available(self) -> bool:
        return bool(_load_ops_config().get("topology", {}).get("enabled", False))

    def initialize(self, session_id: str, **kwargs) -> None:
        home = kwargs.get("hermes_home")
        self._hermes_home = Path(home) if home else None
        if self._hermes_home is not None:
            topo, path = _load_topology_l1(self._hermes_home)
            if topo is None:
                logger.warning(
                    "topo provider active but topology.yaml missing at %s — "
                    "TOPO 段将保持空。", path,
                )

    def system_prompt_block(self) -> str:
        if getattr(self, "_hermes_home", None) is None:
            return ""
        return render_topo_block(self._hermes_home)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Tools ship via the registry ``topo`` toolset; returning [] avoids
        # double registration with the toolset path.
        return []


def register(ctx) -> None:
    ctx.register_memory_provider(TopoMemoryProvider())
