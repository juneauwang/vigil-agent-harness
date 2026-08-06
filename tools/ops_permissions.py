"""Ops Agent Harness — 命令分级权限矩阵（L2/L3 层）。

执行层分级 gate（ops-agent-harness.md §3）：把终端命令分级为 L1-L4，
再按当前环境（test/uat/prod）查矩阵 → 执行 / 审批 / 拒绝。

- ``deny`` 是硬拒绝：不依赖 agent 自觉，任何会话级 bypass（yolo / mode=off /
  永久 allowlist）都不能绕过（由 tools/approval.py 放在 yolo 检查之前调用）。
- ``approve`` 表示需要审批：复用现有 dangerous-command 审批流程。
- 命令不在任何等级 → 返回 None，交回原有检查（等效执行）。

配置（config.yaml，ops 块）:
    ops:
      permissions:
        enabled: true          # 关闭则本模块完全静默
        env: test              # 当前操作环境: test | uat | prod
        role: test             # 会话角色（默认同 env，审计展示用）
        grades:                # 可选：覆盖内置分级正则（不写则用内置表）
          L1: [...]
          L2: [...]
          L3: [...]
          L4: [...]
        matrix:                # 可选：覆盖内置矩阵（不写则用内置矩阵）
          test: {L1: execute, L2: execute, L3: execute, L4: execute}
          uat:  {L1: execute, L2: execute, L3: approve, L4: deny}
          prod: {L1: execute, L2: approve, L3: deny, L4: deny}
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 等级按严重度从高到低匹配（L4 先查，命中即止）。
_GRADE_ORDER = ("L4", "L3", "L2", "L1")

# 内置分级（regex，大小写不敏感）。L1 额外要求：无 shell 操作符、无重定向、
# 无 find -delete/-exec/-ok、无 curl 写操作 —— 保证"查询级"名副其实。
_DEFAULT_GRADES: Dict[str, List[str]] = {
    "L4": [
        # 删 namespace / 删库 / 格式化（致命）
        r"\bkubectl\s+(?:delete|scale\s+--replicas=0)\s+(?:namespace|ns)\b",
        r"\bkubectl\s+delete\s+--all\b",
        r"\b(?:DROP|DELETE)\s+(?:DATABASE|SCHEMA)\b",
        r"\bmkfs(?:\.|\s|$)",
        r"\bformat\s+(?:[a-z]:|[a-z]\d\b|/dev/)",
        r"\bdd\s+.*\bof=/dev/",
    ],
    "L3": [
        # 危险：rm -rf / iptables / 重启 DB / 改配置
        r"\brm\s+(-[^\s]*\s+)*-[^\s]*r",
        r"\brm\s+--recursive\b",
        r"\b(?:iptables|ip6tables|nft)\b",
        r"\bsystemctl\s+(?:restart|stop|disable|mask)\s+(?:mysql|mariadb|postgres(?:ql)?|redis|mongo(?:db)?)\b",
        r"\bservice\s+(?:mysql|mariadb|postgres(?:ql)?|redis|mongo(?:db)?)\s+(?:restart|stop)\b",
        r"\bkubectl\s+(?:apply|edit|delete|patch|rollout\s+undo)\b",
        r"\bsed\s+-i\b",
        r"\bchmod\s+(-[^\s]*\s+)*(?:777|666)\b",
        r"\breboot\b|\bshutdown\b|\binit\s+[06]\b",
        r"\bhelm\s+(?:upgrade|install|rollback|delete|uninstall)\b",
        r"\bdocker\s+(?:rmi|volume\s+rm|system\s+prune|network\s+rm)\b",
    ],
    "L2": [
        # 常规：重启自研服务 / 装包 / 滚动发布
        r"\bsystemctl\s+(?:restart|start|stop|reload|enable)\b",
        r"\bservice\s+(?:restart|start|stop|reload)\b",
        r"\b(?:pip|pip3)\s+install\b",
        r"\b(?:apt|apt-get|yum|dnf)\s+(?:install|remove|purge|upgrade|update)\b",
        r"\bnpm\s+(?:install|ci|uninstall|run\s+deploy)\b",
        r"\bdocker\s+compose\s+(?:up|restart|down)\b",
        r"\bdocker\s+(?:start|restart|stop|build|push)\b",
        r"\bkubectl\s+rollout\s+restart\b",
        r"\bkubectl\s+set\s+image\b",
        r"\bkubectl\s+drain\b",
        r"\bhelm\s+upgrade\b",
        r"\bgit\s+push\b",
        r"\bgit\s+merge\b|\bgit\s+rebase\b|\bgit\s+reset\b",
        r"\bscp\b|\brsync\b",
    ],
    "L1": [
        r"^\s*(?:ls|df|du|pwd|whoami|id|uname|uptime|free|ps|top|htop|date|hostname|stat)\b",
        r"^\s*(?:cat|head|tail|grep|find|which|type|file|tree)\b",
        r"^\s*(?:netstat|ss|ip\s+a|ifconfig|route)\b",
        r"^\s*(?:systemctl\s+status|journalctl|service\s+\S+\s+status)\b",
        r"^\s*(?:docker\s+(?:ps|logs|images|inspect|stats)|kubectl\s+(?:get|describe|logs|top))\b",
        r"^\s*(?:curl|wget)\b",
        r"^\s*(?:echo|printf)\b",
    ],
}

# 内置矩阵：等级 × 环境 → execute / approve / deny（ops-agent-harness.md §3）。
_DEFAULT_MATRIX: Dict[str, Dict[str, str]] = {
    "test": {"L1": "execute", "L2": "execute", "L3": "execute", "L4": "execute"},
    "uat":  {"L1": "execute", "L2": "execute", "L3": "approve", "L4": "deny"},
    "prod": {"L1": "execute", "L2": "approve", "L3": "deny", "L4": "deny"},
}

# L1 例外：出现这些片段就不能算纯查询。
_L1_EXCLUSIONS = [
    r"[;&|]",
    r">>?",
    r"find\s+.*\s-(?:delete|exec|execdir|ok)\b",
    r"curl\b[^\n]*\s-(?:X|d|data|F|form|upload-file)\b",
    r"wget\b[^\n]*\s-(?:O|post-data)\b",
    r"\becho\b.*\s>\s*[/~.]",
]
_L1_EXCLUSIONS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _L1_EXCLUSIONS]


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}).get("permissions", {}) or {}
    except Exception:
        return {}


def _compile_grades(grade_patterns: Dict[str, List[str]]) -> Dict[str, List[re.Pattern]]:
    compiled: Dict[str, List[re.Pattern]] = {}
    for grade, patterns in grade_patterns.items():
        if grade not in _GRADE_ORDER:
            continue
        compiled[grade] = []
        for p in patterns:
            try:
                compiled[grade].append(re.compile(p, re.IGNORECASE))
            except re.error as exc:
                logger.warning("ops_permissions: bad pattern for %s (%r): %s", grade, p, exc)
    return compiled


def classify_command(command: str) -> Optional[str]:
    """Return the highest-severity grade (L4..L1) a command matches, or None."""
    config = _load_config()
    raw_grades = config.get("grades") or {}
    grade_patterns = {g: raw_grades.get(g) or _DEFAULT_GRADES[g] for g in _GRADE_ORDER}
    compiled = _compile_grades(grade_patterns)

    lowered = command.lower()
    for grade in _GRADE_ORDER:
        patterns = compiled.get(grade) or []
        for pattern in patterns:
            if pattern.search(lowered):
                if grade == "L1" and any(
                    excl.search(lowered) for excl in _L1_EXCLUSIONS_COMPILED
                ):
                    continue  # 带操作符/重定向/破坏性选项 → 不是纯查询，继续往高等级查
                return grade
    return None


def _active_env() -> str:
    config = _load_config()
    return str(config.get("env") or "").strip().lower()


def _active_role() -> str:
    config = _load_config()
    return str(config.get("role") or _active_env() or "").strip().lower()


def check_ops_command_permission(command: str) -> Optional[Dict[str, Any]]:
    """Grade a terminal command and apply the environment matrix.

    Returns:
      None                         — gate disabled / env unknown / grade execute
      {"action": "approve", ...}   — 该命令按矩阵需要审批
      {"action": "deny", ...}      — 该命令按矩阵被硬拒绝

    Only plain strings are graded; multi-command shell scripts are left to the
    existing dangerous-command detection (their first token rarely matches a
    grade pattern, and they may hide anything).
    """
    if not isinstance(command, str) or not command.strip():
        return None
    config = _load_config()
    if not config.get("enabled", False):
        return None

    env = _active_env()
    if env not in _DEFAULT_MATRIX and env not in (config.get("matrix") or {}):
        return None  # 未声明环境 → 不做矩阵判定（交回原有检查）

    grade = classify_command(command)
    if grade is None:
        return None

    # Deep-copy: a shallow copy would alias _DEFAULT_MATRIX rows, and the
    # user-matrix merge below would mutate the module default for every later
    # call (first config with a matrix override poisons the built-in table).
    matrix = {env_name: dict(row) for env_name, row in _DEFAULT_MATRIX.items()}
    user_matrix = config.get("matrix") or {}
    for env_name, row in user_matrix.items():
        if isinstance(row, dict):
            matrix.setdefault(env_name, {}).update(row)

    action = matrix.get(env, {}).get(grade, "execute")
    if action == "execute":
        return None

    description = (
        f"命令分级 {grade}（{_grade_examples(grade)}）在 {env} 环境的权限矩阵"
        f"判定为 {'需要审批' if action == 'approve' else '拒绝'}"
    )
    return {
        "action": action,
        "grade": grade,
        "env": env,
        "role": _active_role(),
        "description": description,
    }


def _grade_examples(grade: str) -> str:
    return {
        "L1": "查询",
        "L2": "常规（重启服务/装包）",
        "L3": "危险（rm -rf/iptables/重启DB/改配置）",
        "L4": "致命（删namespace/删库/格式化）",
    }.get(grade, "")
