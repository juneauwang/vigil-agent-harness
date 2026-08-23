"""ansible inventory 契约化守卫（YAPL P2，OPS-DELTA #68）——堵 inventory 绕行。

设计（yapl-design.md §六）：**inventory 不是独立来源，是拓扑表的派生物**。任何
执行路径的主机来源必须唯一（拓扑表），否则就是双份事实（公司 LLM 曾用
``ansible -i`` 绕开拓扑表：凭据/审批守了，target 用 inventory 绕了管辖视野）。

拦截逻辑（校验模式起步，§六"处理三选一"）：
  命令含 ``-i <inventory 文件>`` → 解析 inventory 内容主机集合（ini/yaml）→
  与拓扑表主机比对（name 或 endpoint 匹配）→ 有主机不在拓扑表 = 拒绝执行，
  报错列出缺失主机 + 引导补拓扑或改用拓扑表主机。
  fail-closed：inventory 文件读不到 / 格式不识别 / 拓扑表读不到 → 一律拒绝。

P2 过渡期边界（OPS-DELTA #68 注明）：只拦显式 ``-i``；不带 ``-i`` 的默认
inventory（/etc/ansible/hosts）留 P5 操作分类层；不做全量 terminal 命令分类。
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import yaml

logger = logging.getLogger(__name__)

_ANSIBLE_BINS = frozenset({"ansible", "ansible-playbook"})
_INVENTORY_FLAGS = ("-i", "--inventory", "--inventory-file")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _is_ansible_command(command: str) -> bool:
    """命令首 token（可带 sudo/env 前缀）是 ansible/ansible-playbook。"""
    try:
        tokens = shlex.split(command)
    except Exception:
        return False
    while tokens and tokens[0] in ("sudo", "env", "command"):
        tokens.pop(0)
    while tokens and tokens[0].startswith("sudo"):
        tokens.pop(0)
    if tokens and tokens[0] in ("env",):
        tokens.pop(0)
        while tokens and "=" in tokens[0]:
            tokens.pop(0)
    return bool(tokens) and tokens[0] in _ANSIBLE_BINS


def _extract_inventory_arg(command: str) -> Optional[str]:
    """从命令里取 -i/--inventory(-file) 的 inventory 路径；无 -i → None。"""
    try:
        tokens = shlex.split(command)
    except Exception:
        return None
    for i, tok in enumerate(tokens):
        if tok in _INVENTORY_FLAGS:
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return None
        for flag in ("--inventory-file=", "--inventory=", "-i="):
            if tok.startswith(flag) and len(tok) > len(flag):
                return tok[len(flag):]
        if tok.startswith("-i") and not tok.startswith("--") and len(tok) > 2:
            return tok[2:]
    return None


def _resolve_inventory_path(raw: str, home: Optional[Path]) -> Optional[Path]:
    p = Path(raw)
    if p.is_absolute():
        return p
    if home is not None:
        cand = (Path(home) / raw).resolve()
        if cand.is_file():
            return cand
    return Path(raw).resolve()


def _walk_yaml_inventory(node: Any, hosts: Set[str]) -> None:
    if not isinstance(node, dict):
        return
    if "hosts" in node:
        h = node["hosts"]
        if isinstance(h, dict):
            for name in h.keys():
                if isinstance(name, str) and name.strip():
                    hosts.add(name.strip())
        elif isinstance(h, list):
            for name in h:
                if isinstance(name, str) and name.strip():
                    hosts.add(name.strip())
    for child in (node.get("children") or {}).values():
        _walk_yaml_inventory(child, hosts)
    for key, val in node.items():
        if key not in ("hosts", "children") and isinstance(val, dict) \
                and ("hosts" in val or "children" in val):
            _walk_yaml_inventory(val, hosts)


def _parse_inventory_ini(text: str) -> Set[str]:
    hosts: Set[str] = set()
    in_vars_section = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _SECTION_RE.match(line)
        if m:
            in_vars_section = m.group(1).strip().endswith(":vars")
            continue
        if in_vars_section:
            continue
        if "=" in line and not re.search(r"ansible_host\s*=", line):
            continue  # 变量行（ansible_connection=ssh 等），不是主机
        first = line.split()[0].split(":")[0]
        if first and re.search(r"[A-Za-z0-9]", first):
            hosts.add(first)
        m_host = re.search(r"ansible_host\s*=\s*['\"]?([^'\"\s]+)", line)
        if m_host:
            hosts.add(m_host.group(1))
    return hosts


def parse_inventory(text: str) -> Optional[Set[str]]:
    """解析 inventory 内容主机集合（ini/yaml）；无主机/不识别 → None（fail-closed）。"""
    hosts: Set[str] = set()
    try:
        data = yaml.safe_load(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        _walk_yaml_inventory(data, hosts)
        if hosts:
            return hosts
    ini_hosts = _parse_inventory_ini(text)
    hosts |= ini_hosts
    return hosts or None


def _topology_known_hosts(home: Optional[Path]) -> Tuple[Set[str], bool]:
    """拓扑表主机集合（name + endpoint 主机部分）；拓扑不可读 → (空, False)。"""
    try:
        from tools.topo_tools import load_topology
        topo = load_topology(home)
    except Exception:
        return set(), False
    if topo is None:
        return set(), False
    known: Set[str] = set()
    for h in topo.get("hosts") or []:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        if name:
            known.add(name)
        ep = str(h.get("endpoint") or "").strip()
        if ep:
            known.add(ep.split(":")[0])
    return known, bool(known)


def check_ansible_inventory_guard(command: str,
                                  home: Optional[Path] = None) -> Optional[str]:
    """ansible -i 命令的 inventory 校验；返回拒绝原因（str）或 None（放行）。

    拦截点调用：approval.check_all_command_guards（terminal 全路径）+ sudo_exec。
    """
    if not _is_ansible_command(command):
        return None
    inventory = _extract_inventory_arg(command)
    if inventory is None:
        # P2 过渡期只拦显式 -i（任务书范围）；默认 inventory 留 P5 分类层。
        return None
    path = _resolve_inventory_path(inventory, home)
    if path is None or not path.is_file():
        return (
            f"BLOCKED: ansible inventory 文件读不到: {inventory!r}（fail-closed）。"
            "inventory 不是独立来源，是拓扑表的派生物——请改用拓扑表主机，或确认"
            " inventory 文件路径真实存在。"
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return (
            f"BLOCKED: ansible inventory 文件读取失败: {path}（{exc}）——fail-closed，不放过。"
        )
    hosts = parse_inventory(text)
    if not hosts:
        return (
            f"BLOCKED: ansible inventory 格式无法识别（ini/yaml 均未解析出主机）: {path}"
            "——fail-closed，不放过。"
        )
    known, topo_ok = _topology_known_hosts(home)
    if not topo_ok:
        return (
            "BLOCKED: 无法读取拓扑表（load_topology 失败）——ansible inventory 校验"
            "fail-closed，不放过。先 topo_query 确认拓扑表可用。"
        )
    missing = sorted(
        h for h in hosts
        if h not in known and h.split(":")[0] not in known
    )
    if missing:
        return (
            f"BLOCKED: ansible inventory 含拓扑表外主机: {', '.join(missing)}。"
            "inventory 不是独立来源，是拓扑表的派生物（yapl-design.md §六）——"
            "请补拓扑（topo_discover/topo_query）或改用拓扑表主机（"
            + ", ".join(sorted(known)) + "）。"
        )
    logger.info("ansible inventory guard: %s 主机全部在拓扑表内，放行", path)
    return None
