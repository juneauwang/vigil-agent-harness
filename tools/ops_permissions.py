"""Ops Agent Harness — 命令分级权限矩阵（L2/L3 层）。

执行层分级 gate（ops-agent-harness.md §3）：把终端命令分级为 L1-L4，
再按当前环境（ops.environments 已定义列表）查矩阵 → 执行 / 审批 / 拒绝。

- ``deny`` 是硬拒绝：不依赖 agent 自觉，任何会话级 bypass（yolo / mode=off /
  永久 allowlist）都不能绕过（由 tools/approval.py 放在 yolo 检查之前调用）。
- ``approve`` 表示需要审批：复用现有 dangerous-command 审批流程。
- 命令不在任何等级 → 返回 None，交回原有检查（等效执行）。

环境枚举固定为 local/test/dev/prod 四值（OPS-DELTA #42）。老自定义名
（uat/staging/bare_metal_prod/...）读取时按档位映射、不报错：uat→prod 档、
staging→dev 档、其余按环境定义 isolation（strict→prod、relaxed→dev）或名字
推导——映射只打一次警告，权限语义不放松（uat→prod 只会更严不会更松）。
config 的 ``ops.environments`` 是 /env 的权威名单（老自定义名映射为档位），
拓扑 topology.yaml 的 environments 段不一致时以 config 为准。

矩阵默认启用（OPS-DELTA #1）：``ops.permissions.enabled`` 缺省视为 true（显式
``false`` 仍可关闭，向后兼容），env 缺省读 config 的 ``ops.permissions.env``——
未配置 env（非 ops profile）时矩阵惰性返回 None（交回原有检查），不改变既有
判定；一旦 env 就位（ops-init 默认 test 或 /env 切换）即按矩阵判定。fail-closed
取向不变：DENY 是硬拒绝，任何会话级 bypass 都不能绕过。

配置（config.yaml，ops 块）:
    ops:
      environments:            # 可选：环境定义列表（不写则用内置四值 local/test/dev/prod）
        - {name: test, isolation: relaxed, role: test}
        - {name: bare_metal_prod, isolation: strict, role: prod}   # 老自定义名 → 映射 prod 档
      permissions:
        enabled: true          # 缺省默认启用（OPS-DELTA #1）；显式 false 才关闭
        env: test              # 当前操作环境（/env 切换，四值 local/test/dev/prod；老名按档位映射）
        role: test             # 会话角色（默认同 env，审计展示用）
        grades:                # 可选：覆盖内置分级正则（不写则用内置表）
          L1: [...]
          L2: [...]
          L3: [...]
          L4: [...]
        matrix:                # 可选：按 env 名覆盖内置矩阵（不写则用内置矩阵）
          test: {L1: execute, L2: execute, L3: execute, L4: execute}
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

# 内置矩阵：等级 × 档位 → execute / approve / deny（ops-agent-harness.md §3）。
# 四值枚举 local/test/dev/prod（OPS-DELTA #42）：local/test/dev relaxed 全放行，
# prod strict（L2 审批 / L3、L4 拒绝）。老 uat 行已删除——legacy uat 经档位
# 映射到 prod 行（更严不更松），不再有独立的 uat 档。
_DEFAULT_MATRIX: Dict[str, Dict[str, str]] = {
    "local": {"L1": "execute", "L2": "execute", "L3": "execute", "L4": "execute"},
    "test":  {"L1": "execute", "L2": "execute", "L3": "execute", "L4": "execute"},
    "dev":   {"L1": "execute", "L2": "execute", "L3": "execute", "L4": "execute"},
    "prod":  {"L1": "execute", "L2": "approve", "L3": "deny", "L4": "deny"},
}

# 内置环境定义（config 未写 ops.environments 时的兜底，与 _CONFIG_TPL 生成一致）。
# isolation 语义沿用 topology/ops-agent-harness.md：strict = 跨环境操作需审批，
# relaxed = 自用放行；矩阵行为由档位决定，isolation 为声明性展示字段。
_DEFAULT_ENVIRONMENTS: List[Dict[str, str]] = [
    {"name": "local", "isolation": "relaxed", "role": "local"},
    {"name": "test", "isolation": "relaxed", "role": "test"},
    {"name": "dev", "isolation": "relaxed", "role": "dev"},
    {"name": "prod", "isolation": "strict", "role": "prod"},
]

# 环境四值枚举 + 老自定义名档位映射（OPS-DELTA #42）。
_ENV_TIERS: tuple = ("local", "test", "dev", "prod")
_LEGACY_ENV_TIER_MAP: Dict[str, str] = {"uat": "prod", "staging": "dev"}
# 每个 legacy 名只警告一次（读取兼容：警告不阻断，老数据必须可读）。
_WARNED_ENVS: set = set()

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

# 变更类命令清单（OPS-DELTA #32 prod 变更强制确认门）：服务重启 / 容器重建 /
# 配置下发类。env=prod 时命中即 require_confirmation（approve 决策强制走人工
# 确认门）；非 prod 档不触发（现状不变）。宁可多列——漏列的代价是 prod 变更
# 无确认执行，多列的代价只是 prod 变更命令多一次人工确认。
_CHANGE_COMMAND_RE = re.compile(
    r"\bansible-playbook\b"
    r"|\bkubectl\s+(?:apply|delete|edit|scale|rollout|drain|cordon)\b"
    r"|\bdocker\s+compose\s+(?:up|restart|rm|down)\b"
    r"|\bdocker\s+(?:restart|rm|stop)\b"
    r"|\bsystemctl\s+(?:restart|stop)\b"
    r"|\bhelm\s+(?:upgrade|install|uninstall)\b",
    re.IGNORECASE,
)


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}).get("permissions", {}) or {}
    except Exception:
        return {}


def _load_ops_config() -> Dict[str, Any]:
    """ops 块整体（permissions 是子块；environments 与 permissions 平级）。"""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}) or {}
    except Exception:
        return {}


def defined_environments(ops_config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """已定义环境列表：四值档位 local/test/dev/prod（OPS-DELTA #42）。

    这是 /env 命令与权限矩阵共用的权威名单：config ops.environments 里的
    老自定义名（uat/staging/...）读取时按档位映射（打一次警告，不阻断），
    映射后的档位为准——权限语义不放松（uat→prod 只会更严）。config 未定义
    时回退内置四值。
    """
    ops = ops_config if ops_config is not None else _load_ops_config()
    envs = ops.get("environments") or []
    if not isinstance(envs, list) or not envs:
        return [dict(e) for e in _DEFAULT_ENVIRONMENTS]
    seen: Dict[str, Dict[str, Any]] = {}
    for e in envs:
        if not isinstance(e, dict):
            continue
        raw_name = str(e.get("name") or "").strip()
        if not raw_name:
            continue
        tier = _map_env_tier(raw_name, ops)
        if tier in seen:
            continue
        if raw_name.lower() in _ENV_TIERS:
            seen[tier] = dict(e)
        else:
            canonical = next(d for d in _DEFAULT_ENVIRONMENTS if d["name"] == tier)
            seen[tier] = dict(canonical)
    return [seen[t] for t in _ENV_TIERS if t in seen] or [
        dict(e) for e in _DEFAULT_ENVIRONMENTS
    ]


def _raw_env_definition(env: str, ops_config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按名（大小写不敏感）查 config ops.environments 的原始定义（未映射）。

    供档位映射读取 isolation/role（老自定义名映射时保留声明的权限语义）。
    """
    ops = ops_config if ops_config is not None else _load_ops_config()
    envs = ops.get("environments") or []
    if not isinstance(envs, list):
        return None
    for e in envs:
        if isinstance(e, dict) and str(e.get("name") or "").strip().lower() == env:
            return e
    return None


def _map_env_tier(env: str, ops_config: Optional[Dict[str, Any]] = None) -> str:
    """env 名 → 四值档位（local/test/dev/prod）。

    四值原样返回；老自定义名按档位映射：uat→prod、staging→dev；其余按
    原始定义 isolation 推导（strict→prod、relaxed→dev，role 为四值时 role 优先），
    无定义按名字推导（含 prod → prod、尾缀 local/test/dev → 对应档、默认 dev）。
    映射只打一次警告（logger.warning），不阻断读取——老数据必须可读。
    """
    lower = str(env or "").strip().lower()
    if lower in _ENV_TIERS:
        return lower
    mapped = _LEGACY_ENV_TIER_MAP.get(lower)
    if mapped is None:
        env_def = _raw_env_definition(lower, ops_config)
        if env_def:
            role = str(env_def.get("role") or "").strip().lower()
            if role in _ENV_TIERS:
                mapped = role
            else:
                isolation = str(env_def.get("isolation") or "").strip().lower()
                mapped = "prod" if isolation == "strict" else "dev"
        elif "prod" in lower:
            mapped = "prod"
        elif lower.endswith("local"):
            mapped = "local"
        elif lower.endswith("test"):
            mapped = "test"
        elif lower.endswith("dev"):
            mapped = "dev"
        else:
            mapped = "dev"
    if lower not in _WARNED_ENVS:
        _WARNED_ENVS.add(lower)
        logger.warning(
            "ops_permissions: 环境 %r 不在枚举 %s 内，按档位映射为 %s（权限语义不放松）",
            env, "/".join(_ENV_TIERS), mapped,
        )
    return mapped


def _env_definition(env: str, ops_config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按名（大小写不敏感）查已定义环境；未定义返回 None。"""
    for env_def in defined_environments(ops_config):
        if str(env_def.get("name") or "").strip().lower() == env:
            return env_def
    return None


def _env_role(env: str, ops_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """环境定义的 role（决定矩阵行为落点）；未定义或无 role 返回 None。"""
    env_def = _env_definition(env, ops_config)
    if env_def is None:
        return None
    return str(env_def.get("role") or "").strip().lower() or None


def _matrix_row(config: Dict[str, Any], env: str,
                ops_config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """env 名 → 有效矩阵行（深拷贝，返回 None 表示未声明 → 交回原有检查）。

    查表顺序（四值枚举，矩阵行为由档位决定）：
      1. 用户 ``matrix`` 按 env 名覆盖（在默认行基础上合并，行为同旧版 setdefault+update）；
      2. 已定义环境：按定义的 role 落点（bare_metal_prod → role prod → prod 行）；
      3. 老自定义名（uat/staging/...）按档位映射（_map_env_tier，打一次警告；
         uat→prod 档，权限只会更严不会更松）；
      4. 内置名兜底（兼容旧矩阵）。
    """
    user_matrix = config.get("matrix") or {}
    if isinstance(user_matrix.get(env), dict):
        row = dict(_DEFAULT_MATRIX.get(env, {}))
        row.update(user_matrix[env])
        return row or None
    role = _env_role(env, ops_config)
    if role:
        row = dict(_DEFAULT_MATRIX.get(role, {}))
        if row:
            return row
    # 老自定义名（uat/staging/...）→ 档位映射（打一次警告；uat→prod 更严不更松）。
    tier = _map_env_tier(env, ops_config)
    row = dict(_DEFAULT_MATRIX.get(tier, {}))
    if row:
        return row
    return dict(_DEFAULT_MATRIX.get(env, {})) or None


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


def _is_prod_env(env: str, ops_config: Optional[Dict[str, Any]] = None) -> bool:
    """env 是否按 prod 档处理（决定 require_confirmation 门）。

    与环境定义里的 role 对齐（bare_metal_prod → role prod → prod 档）；
    未定义/老自定义名按档位映射（uat → prod 档，权限语义不放松）。
    """
    role = _env_role(env, ops_config)
    if role:
        return role == "prod"
    return _map_env_tier(env, ops_config) == "prod"


def check_ops_command_permission(command: str, target_env: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Grade a terminal command and apply the environment matrix.

    ``target_env`` overrides the session env（目标级 env 判定）: approval.py
    先做命令目标解析（tools/ops_target.py），命中拓扑实体时传入该实体的 env；
    为 None 或未命中时用会话 env（现状不变）。目标 env 未在矩阵声明时同样返回
    None（交回原有检查），避免对未知环境误判。

    OPS-DELTA #32：decision 增加 ``require_confirmation`` 字段——变更类命令
    （服务重启/容器重建/配置下发，见 ``_CHANGE_COMMAND_RE``）在 env 按 prod 档
    判定（含映射的 uat）时默认 true，approval.py 对 approve+require_confirmation
    强制走人工确认门（yolo / smart-approval / 永久 allowlist 都不能绕过）。
    L3/L4 的 deny 仍硬拒，优先级不变（deny > require_confirmation）；未分级的
    变更类命令在 prod 档合成审批级判定（否则 ansible-playbook 这类未列入分级
    的命令会漏网）。

    OPS-DELTA 批次十七（B'）：prod 档**未分级命令**（grade=None，不在 L1-L4
    任何模式内，如 ``mv 单文件`` / ``cp`` / ``tar`` / 自定义脚本）默认
    approve + require_confirmation=True（强制人工确认门，同 #32 变更门通道，
    yolo 也绕不过）——堵"批量危险操作拆成单条无害命令全绕过"（2026-08-13
    k3s-prod 改名事故：mv 跨 20+ 文件全部放行）。L1 查询命令匹配 L1 模式
    （grade 非 None）不受影响，prod 仍 execute；L3/L4 deny 优先级不变；
    非 prod 档（local/test/dev）未分级命令维持现状放行。

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
    if config.get("enabled", True) is False:
        return None  # 显式关闭（向后兼容）；缺省默认启用

    env = (target_env or _active_env()).strip().lower()
    if not env:
        # 无会话 env 且无目标级 env → 矩阵无可判定环境（等价于 _matrix_row("")
        # 返回 None），提前返回：非 ops profile 的热路径零额外开销。
        return None

    grade = classify_command(command)
    was_ungraded = grade is None
    ops_config = _load_ops_config()
    row = _matrix_row(config, env, ops_config) if grade is not None else None

    env_tier = _map_env_tier(env, ops_config)
    is_prod = _is_prod_env(env, ops_config)
    is_change_cmd = bool(_CHANGE_COMMAND_RE.search(command))
    require_confirmation = bool(is_prod and is_change_cmd)

    if grade is not None and row is not None:
        action = row.get(grade, "execute")
        if action == "execute":
            # 未命中审批/拒绝档：除非是 prod 变更类（强制确认门），否则交回
            # 原有检查（现状）。
            if not require_confirmation:
                return None
            action = "approve"
    else:
        # 未分级（grade=None）或未声明环境：
        # B'（OPS-DELTA 批次十七）：prod 档未分级命令默认 approve + 强制人工
        # 确认门——堵"批量危险操作拆成单条 mv/cp/tar 绕过"（2026-08-13
        # k3s-prod 改名事故，mv 单文件不在任何分级模式被全放行）。L1 查询命令
        # 匹配 L1 模式（grade 非 None）不受影响；L3/L4 deny 优先级不变。
        # 非 prod 档（local/test/dev）维持现状：未分级命令交回原检查放行。
        if not is_prod:
            if not require_confirmation:
                return None
            action = "approve"
        else:
            action = "approve"
            require_confirmation = True  # prod 未分级 = 强制人工确认门（同变更类）
        grade = grade or "L2"

    if was_ungraded and is_prod and action == "approve":
        # B' 文案（要点 4）：未分级 + prod 明示"未分级命令在 prod 需人工确认"。
        # 保留 "prod 变更确认门" 字样——approval 侧测试与用户提示都依赖它。
        description = (
            f"⚠ prod 变更确认门（未分级命令在 prod 需人工确认，B'）：{command} "
            "不在 L1-L4 分级模式内，按 prod 档默认审批"
        )
    else:
        description = (
            f"命令分级 {grade}（{_grade_examples(grade)}）在 {env} 环境的权限矩阵"
            f"判定为 {'需要审批' if action == 'approve' else '拒绝'}"
        )
        if require_confirmation:
            description = f"⚠ prod 变更确认门：{description}"
    return {
        "action": action,
        "grade": grade,
        "env": env,
        "env_tier": env_tier,
        "role": _active_role(),
        "require_confirmation": require_confirmation,
        "description": description,
    }


def _grade_examples(grade: str) -> str:
    return {
        "L1": "查询",
        "L2": "常规（重启服务/装包）",
        "L3": "危险（rm -rf/iptables/重启DB/改配置）",
        "L4": "致命（删namespace/删库/格式化）",
    }.get(grade, "")
