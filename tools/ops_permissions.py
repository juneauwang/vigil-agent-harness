"""Ops Agent Harness — 操作矩阵权限判定（YAPL P5 收口，OPS-DELTA #75）。

L1-L4 命令分级**退役**（yapl-design.md §11.5）：判定对象从"命令正则分级"
换成"动作枚举 × 操作矩阵"——terminal 直跑的命令先过操作分类层
（tools/action_classifier.py）归成动作枚举（schemas.yaml actions 23 个），
再按动作 × env 查操作矩阵（tools/matrix_data.py，与 runbook 执行路径同一
matrix.yaml——双矩阵统一）。本模块保留被其他模块引用的环境辅助函数：

- ``defined_environments`` / ``_map_env_tier`` / ``_raw_env_definition`` /
  ``_LEGACY_ENV_TIER_MAP``：/env 命令（cli.py）、runbook env 校验
  （tools/runbook_tools.py）与矩阵 env 名解析继续使用；老自定义名
  （uat/staging/...）按档位映射的兼容语义不变（更严不更松）。
- ``check_ops_command_permission``：统一入口（approval.py terminal 审批链 /
  sudo_tool / chat_api / web_server 探测共用），内部走 classifier → 矩阵。

矩阵语义（§11.2 设计铁律）：
- 矩阵无 deny：classifier 不产出 deny，保守 = approve（走审批门）不是拒绝；
  硬底线 / sudo stdin / 用户 deny / ansible inventory guard 等无条件层由
  approval.py 在本函数之前执行（顺序不动）。
- execute → 返回 None（交回原有检查，直接执行）；approve → 返回
  {"action": "approve", ...} 走现有审批门（approvals.mode smart/manual）；
  {approve: required} → require_confirmation=True 强制人工（覆盖 mode）。
- batch80（OPS-DELTA #95）执行器绕行后门收口：prod 环境 + 变更类动作
  （非只读）→ 强制 require_confirmation=True（覆盖 smart 自动批），防 LLM
  回退 terminal 用裸命令绕过 runbook 执行器矩阵裁决；unknown/只读/非 prod
  不额外强制（行为边界见 check_ops_command_permission）。
- 动作识别不出（unknown）→ 矩阵漏配 → 默认 approve（保守）+ warning。
- 动作漏配（矩阵没配该 action×env）→ get_level 默认 approve（保守）。

配置（config.yaml，ops 块）:
    ops:
      environments:            # 可选：环境定义列表（/env 名单，老名按档位映射）
        - {name: test, isolation: relaxed, role: test}
        - {name: bare_metal_prod, isolation: strict, role: prod}
      permissions:
        enabled: true          # 缺省默认启用（OPS-DELTA #1）；显式 false 才关闭
        env: test              # 当前操作环境（/env 切换；矩阵按 env 名精确查，
                               #   未配置时按档位映射名 uat→prod 兜底）

矩阵内容（matrix.yaml）由 P3 操作矩阵层管理（``vigil matrix`` CLI/UI），
本模块只读判定，不写矩阵。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 环境四值枚举 + 老自定义名档位映射（OPS-DELTA #42，保留供 /env 与矩阵 env 解析）。
_ENV_TIERS: tuple = ("local", "test", "dev", "prod")
_LEGACY_ENV_TIER_MAP: Dict[str, str] = {"uat": "prod", "staging": "dev"}
# 每个 legacy 名只警告一次（读取兼容：警告不阻断，老数据必须可读）。
_WARNED_ENVS: set = set()

_DEFAULT_ENVIRONMENTS: List[Dict[str, str]] = [
    {"name": "local", "isolation": "relaxed", "role": "local"},
    {"name": "test", "isolation": "relaxed", "role": "test"},
    {"name": "dev", "isolation": "relaxed", "role": "dev"},
    {"name": "prod", "isolation": "strict", "role": "prod"},
]


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

    这是 /env 命令与权限判定共用的权威名单：config ops.environments 里的
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


def all_defined_environments(ops_config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """/env 可用名单（含自定义名）：config ops.environments 定义的全部环境按名字
    原样返回——bare_metal_prod 这类自定义名仍以本名展示/切换。

    与 ``defined_environments``（档位折叠）分工：后者把自定义名映射成四值档位
    供权限判定用；这里保留每个定义的名字本身供 /env 展示与切换（role/isolation
    仍来自原始定义，切换后权限判定侧继续走 ``_map_env_tier`` 档位映射，语义不
    放松——更严不更松）。config 未定义时回退内置四值 local/test/dev/prod。
    """
    ops = ops_config if ops_config is not None else _load_ops_config()
    envs = ops.get("environments") or []
    if not isinstance(envs, list) or not envs:
        return [dict(e) for e in _DEFAULT_ENVIRONMENTS]
    result: List[Dict[str, Any]] = []
    seen: set = set()
    for e in envs:
        if not isinstance(e, dict):
            continue
        raw_name = str(e.get("name") or "").strip()
        if not raw_name:
            continue
        key = raw_name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(e))
    return result or [dict(e) for e in _DEFAULT_ENVIRONMENTS]


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


def _active_env() -> str:
    config = _load_config()
    return str(config.get("env") or "").strip().lower()


def _active_role() -> str:
    config = _load_config()
    return str(config.get("role") or _active_env() or "").strip().lower()


# batch80（OPS-DELTA #95）：prod 变更强制人工确认的动作集合。与 runbook 执行器
# （tools/runbook_exec.py）的 _EXPECT_CHANGE_ACTIONS 同款"变更 vs 只读"划分，
# 另把 apply_config / runbook 也纳入——prod 语义里改配置与嵌套 runbook 同样是
# 变更。unknown 不在集合内 → 不强制（识别不出可能是 ls/echo 等无害命令被误判）。
_MUTATING_ACTIONS: frozenset = frozenset({
    "start", "stop", "restart", "reload", "enable", "disable", "reboot",
    "shutdown", "deploy", "rollback", "scale", "decommission", "backup",
    "restore", "apply_config", "install", "upgrade", "remove", "runbook",
})


def _is_mutating_action(action_name: str) -> bool:
    """变更类动作判定（prod 强制人工门用）：动作词表内 → True。

    只读（query/fetch_log/verify/transfer_file/run_script）不在集合内；
    unknown 不在集合内 → False（不误伤无害命令）。
    """
    return str(action_name or "").strip() in _MUTATING_ACTIONS


def _is_prod_env(env: str) -> bool:
    """prod 档位判定（batch80 强制人工门用）：档位映射 == prod。

    复用 _map_env_tier（uat→prod 等老自定义名同样视为 prod——权限语义不放松，
    与 env_tier 计算同款）。
    """
    try:
        return _map_env_tier(env, _load_ops_config()) == "prod"
    except Exception:
        return False


def _matrix_env_for(env: str, matrix_data: Dict[str, Any]) -> str:
    """矩阵 env 名解析：精确名优先；未配置 → 档位映射名（uat→prod、staging→dev、
    老自定义名按 isolation/role 推导——更严不更松）；仍无 → 原样返回（get_level
    默认 approve 保守）。"""
    matrix = matrix_data.get("matrix") or {}
    if env in matrix:
        return env
    try:
        tier = _map_env_tier(env)
    except Exception:
        return env
    if tier in matrix:
        return tier
    return env


def _matrix_description(command: str, action_name: str, level: str, env: str,
                        primary: Dict[str, Any],
                        classification: Dict[str, Any],
                        prod_hardgate: bool = False) -> str:
    """审批展示文案：动作 × env 档位 / unknown 保守提示 / 链式取保守说明。"""
    from tools import matrix_data as _md
    chain = classification.get("chain") or None
    if level == _md.LEVEL_REQUIRED:
        desc = (
            f"⚠ 操作矩阵强制人工确认（{action_name} × {env} = "
            "{approve: required}，覆盖 approvals.mode）"
        )
    elif prod_hardgate:
        desc = (
            f"⚠ prod 变更强制人工确认（{action_name} × {env}）：terminal 裸命令"
            "不得绕过 runbook 执行器矩阵裁决（OPS-DELTA #95）"
        )
    elif action_name == "unknown":
        desc = (
            f"⚠ 未能识别命令意图（unknown）：{command} 不在操作分类规则表内，"
            "矩阵漏配 → 按保守审批处理（默认 approve 走审批门）；"
            "可在 OPS-DELTA 登记新规则。"
        )
    else:
        desc = f"操作矩阵 {action_name} × {env} 判定为需要审批（approve）"
    rule = str(primary.get("rule") or "").strip()
    if rule:
        desc = f"{desc}（规则 {rule}）"
    note = str(primary.get("note") or "").strip()
    if note and action_name != "unknown":
        desc = f"{desc}；{note}"
    if chain and len(chain) > 1:
        joined = " / ".join(
            f"{c.get('action')}({c.get('rule') or '?'})" for c in chain
        )
        desc = f"{desc}（链式命令取保守：{joined}）"
    return desc


def check_ops_command_permission(command: str, target_env: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """操作矩阵判定（YAPL P5：classifier → 动作枚举 × env 矩阵，OPS-DELTA #75）。

    L1-L4 命令分级退役：判定对象从"命令正则分级"换成"动作枚举 × 操作矩阵"
    （tools/action_classifier.py + tools/matrix_data.py，与 runbook 执行路径
    同一 matrix.yaml——双矩阵统一）。矩阵无 deny：classifier 不产出 deny，
    保守 = approve（走审批门）不是拒绝；硬底线 / sudo stdin / 用户 deny /
    ansible inventory guard 等无条件层由 approval.py 在本函数之前执行。

    档位语义：
      execute → 返回 None（交回原有检查，直接执行）；
      approve → 返回 {"action": "approve", ...} 走现有审批门（smart/manual）；
      {approve: required} → require_confirmation=True 强制人工（覆盖 mode）。
      prod 环境 + 变更类动作（非只读）→ 额外强制 require_confirmation=True
      （batch80，OPS-DELTA #95：执行器绕行后门收口——execute/approve 档位在
      prod 变更时也升到人工确认，覆盖 smart 自动批）。
    batch83（OPS-DELTA #98）目标化裁决：高危变更动作（install/remove/
    decommission/reboot/scale + kubectl delete 类）与 ssh 族受控通道执行前
    强制目标解析（tools/target_resolve.py）——解析出实体 → 实体 env 查矩阵
    （会话声明 env 零参与）；解析失败 → 返回 deny（目标解析层硬拦截，先于
    矩阵查询；矩阵本身仍无 deny 档）。低危变更与只读动作维持原判（target_env
    或会话 env；只读不解析）。
    动作识别不出（unknown）→ 矩阵漏配 → 默认 approve（保守）+ warning。

    Args:
        command: 原始命令串（sudo/doas 前缀由 classifier 剥离）。
        target_env: 命令目标级 env（ops_target 解析，跨环境硬约束）；None →
            会话 env（config ops.permissions.env）。

    Returns:
        None  — gate disabled / env 未配置 / 矩阵档位 execute（交回原有检查）。
        dict  — {"action": "approve", "action_name", "rule", "note", "env",
                 "env_tier", "role", "level", "require_confirmation",
                 "description", "classification"}；高危目标解析失败 →
                 {"action": "deny", ...}（approval.py / sudo_tool 消费为拒绝）。
    """
    if not isinstance(command, str) or not command.strip():
        return None
    config = _load_config()
    if config.get("enabled", True) is False:
        return None  # 显式关闭（向后兼容）；缺省默认启用

    from tools import matrix_data as _md

    # 矩阵未初始化（matrix.yaml 不存在）→ 非 prod 档惰性（回归修复，OPS-DELTA
    # #76）：P5 把 L1-L4 分级换成 classifier 后，unknown → 默认 approve 在矩阵
    # 缺失时把 echo/ls/curl 等普通命令也弹进审批门，破坏 P4 基线（矩阵缺失 →
    # 行 None → 非 prod 未分级命令交回原检查直接放行）。矩阵未初始化时只对
    # prod 档门控（与 P4 B'/变更确认门一致：prod 档默认审批）；local/test/dev
    # 交回原有检查（tirith / 危险命令层兜底）。矩阵一旦初始化（文件存在）→
    # 全量 P5 语义（unknown → 默认 approve）。runbook 路径不受影响（其矩阵
    # 缺失仍按空矩阵 approve 门控，P4 既有，非本批回归面）。
    if not _md.matrix_path().is_file():
        ops_config = _load_ops_config()
        probe_env = (target_env or _active_env()).strip().lower()
        env_def = _raw_env_definition(probe_env, ops_config)
        role = str((env_def or {}).get("role") or "").strip().lower()
        is_prod = role == "prod" if role else _map_env_tier(probe_env, ops_config) == "prod"
        if not is_prod:
            return None

    from tools.action_classifier import classify_command
    from tools.target_resolve import (
        TARGET_RESOLVED,
        command_target_required,
        resolve_required_target,
    )

    classification = classify_command(command)
    candidates = classification.get("chain") or [classification]
    actions = {str(c.get("action") or "") for c in candidates}

    # batch83（OPS-DELTA #98）：高危变更动作 / ssh 族受控通道 → 强制目标解析。
    # 解析出实体 → 实体 env 查矩阵（会话声明 env 不参与高危变更裁决）；解析
    # 失败 → deny（目标解析层硬拦截，发生在查矩阵之前；矩阵本身仍无 deny 档，
    # deny 是解析层的档位，与矩阵 execute/approve/required 三档不冲突）。
    target_label = None
    target_entity = None
    if command_target_required(actions, command):
        target = resolve_required_target(command)
        if target.get("status") != TARGET_RESOLVED:
            return _target_deny_decision(command, classification, target)
        env = str(target.get("env") or "").strip().lower()
        target_label = target.get("label")
        target_entity = target.get("entity")
        if not env:
            return None
    else:
        env = (target_env or _active_env()).strip().lower()
        if not env:
            # 无会话 env 且无目标级 env → 矩阵无可判定环境，交回原检查（热路径零额外开销）。
            return None

    matrix = _md.load_matrix_or_empty()
    resolved_env = _matrix_env_for(env, matrix)
    rank = {_md.LEVEL_EXECUTE: 0, _md.LEVEL_APPROVE: 1, _md.LEVEL_REQUIRED: 2}

    # 链式取保守：每个子命令各自查矩阵，取档位最高（最保守）者；
    # unknown 动作 → 矩阵漏配 → 默认 approve（保守）。
    best = None
    for cand in candidates:
        action = str(cand.get("action") or "unknown")
        if action == "unknown":
            level = _md.LEVEL_APPROVE
        else:
            level = _md.get_level(matrix, resolved_env, action)["level"]
        weight = rank.get(level, 1)
        if best is None or weight > best[0]:
            best = (weight, level, action, cand)
    _weight, level, action_name, primary = best

    # batch80（OPS-DELTA #95）：执行器绕行后门收口——prod 环境 + 变更类动作
    # （非只读）→ 强制人工确认（require_confirmation=True 覆盖 smart 自动批，
    # approval.py 的 _ops_confirmation_required 机制现成）。防 LLM 回退
    # terminal 用裸命令绕过 runbook 执行器矩阵裁决（2026-08-26 实证：kubectl
    # scale / set resources 不在黑名单 → smart 自动批 = 矩阵后门）。
    # 行为边界：非 prod 完全不变（execute 放行、approve 走 smart）；prod +
    # 只读动作不变（诊断不阻）；unknown 不强制（无害命令不误伤）；矩阵已
    # {approve: required} 保持。只把"prod + 变更"从 smart 升到人工，不放松
    # 任何现有门。
    env_tier = _map_env_tier(env, _load_ops_config())
    prod_change_hardgate = env_tier == "prod" and _is_mutating_action(action_name)

    if level == _md.LEVEL_EXECUTE and not prod_change_hardgate:
        return None  # 直接执行（交回原有检查）

    require_confirmation = level == _md.LEVEL_REQUIRED
    if not require_confirmation:
        require_confirmation = prod_change_hardgate
    description = _matrix_description(
        command, action_name, level, env, primary, classification,
        prod_hardgate=prod_change_hardgate,
    )
    result = {
        "action": "approve",
        "action_name": action_name,
        "rule": str(primary.get("rule") or "").strip() or None,
        "note": str(primary.get("note") or "").strip() or None,
        "env": env,
        "env_tier": env_tier,
        "role": _active_role(),
        "level": level,
        "require_confirmation": require_confirmation,
        "description": description,
        "classification": classification,
    }
    if target_label:
        result["target_label"] = target_label
        result["target_entity"] = target_entity
    return result


def _target_deny_decision(command: str, classification: Dict[str, Any],
                          target: Dict[str, Any]) -> Dict[str, Any]:
    """batch83 目标解析失败 → deny（先于矩阵的硬拦截；矩阵本身仍无 deny 档）。

    交互会话里 confirm 会被 smart 自动批绕过（本次 npm 逃逸的机制），deny 才
    成立：解析不出目标实体 = 拒绝执行，错误信息带修复指引（先 topo_query 查
    实体名）。sudo_tool 已按 ``action == "deny"`` 消费（tool_error），
    approval.py 在本批同步加 deny 分支（先于 yolo/mode=off 旁路）。
    """
    action_name = str(classification.get("action") or "unknown")
    reason = str(target.get("reason") or "目标实体未解析/不在拓扑表")
    return {
        "action": "deny",
        "action_name": action_name,
        "rule": str(classification.get("rule") or "").strip() or None,
        "note": str(classification.get("note") or "").strip() or None,
        "env": None,
        "env_tier": None,
        "role": _active_role(),
        "level": None,
        "require_confirmation": False,
        "description": (
            f"⛔ 高危变更目标未解析，操作已拒绝（{action_name} × 目标解析失败）："
            f"{reason}。矩阵按目标实体 env 裁决，解析不出实体 = deny（目标解析层"
            "硬拦截，先于矩阵）；修复指引：先 topo_query 查目标实体名（主机名/"
            "别名/IP/集群上下文），或用 topo_update 登记后再执行。"
        ),
        "classification": classification,
        "target_deny": True,
    }
