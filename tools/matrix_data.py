"""YAPL P3 操作矩阵核心数据层（yapl-design.md §11）。

矩阵是安全资产：独立 ``~/.vigil/matrix.yaml``（与 topology/config 分开——
独立防误改、独立备份、diff 清晰）。**LLM 永不写矩阵**——所有修改走人工通道
（``vigil matrix`` CLI / UI 表格页），本模块只提供数据加载/校验/模板生成/
受控写接口（CLI/UI 内部使用），无任何 LLM 工具入口。LLM 侧只有
``tools/matrix_tools.py`` 的 ``matrix_query`` 只读查询。

设计铁律（§11.2 / 11.7）：
- 矩阵无 deny——出现 deny/denied 等值 = 报错（deny 诱发 LLM 绕行；runbook
  是预审流程，deny 在生成时拦）。
- 动作漏配默认 approve（保守）+ 加载时 warning。
- ``{approve: required}`` 强制人工（高危不 smart，覆盖 approvals.mode）。
- 热生效：任何读取路径实时读 matrix.yaml（不缓存、不预热）；文件缺失 →
  空矩阵（全部动作默认 approve）+ warning。
- 修改即审计：CLI/UI 调用方负责 record_event；本模块返回变更信息。

文件结构：:

    schema_version: 1
    updated_at: 2026-08-23T10:00:00+08:00
    source: template2            # 顶层来源：模板名 or manual（任一格手改后变 manual）
    base_template: template2     # 生成基底模板（reset 回退目标；手改不清除）
    matrix:
      prod:
        query: execute
        restart: {approve: required}
    sources:                     # 每格来源追踪（平行结构：来源 = 模板名 or manual）
      prod:
        query: template2
        restart: template2
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from tools.registry import tool_error

logger = logging.getLogger(__name__)

MATRIX_FILENAME = "matrix.yaml"
SCHEMA_VERSION = 1

# 档位内部规范值（文件形态：execute / approve / {approve: required}）。
LEVEL_EXECUTE = "execute"
LEVEL_APPROVE = "approve"
LEVEL_REQUIRED = "required"
_LEVELS = (LEVEL_EXECUTE, LEVEL_APPROVE, LEVEL_REQUIRED)

# deny 语义黑名单——设计铁律：矩阵无 deny。出现这些值（字符串） = 报错。
_DENY_VALUES = frozenset({
    "deny", "denied", "denies", "block", "blocked", "forbid", "forbidden",
    "no", "none",
})

# 动作词表回退：schemas.yaml 读不到时用 §10.2 文本 23 动作（OPS-DELTA #68
# 已登记标题 24 / 逐数 23 的差异，以文本为准）。
_FALLBACK_ACTIONS = [
    "start", "stop", "restart", "reload", "enable", "disable",
    "reboot", "shutdown",
    "deploy", "rollback", "scale", "decommission",
    "backup", "restore",
    "apply_config",
    "query", "fetch_log", "verify",
    "transfer_file",
    "run_script",
    "install", "upgrade", "remove",
]

# 模板 1/2/3 高危动作（approve/required 语义共用）。
_HIGH_RISK_ACTIONS = ("reboot", "shutdown", "remove", "decommission")
_T2_DEV_APPROVE = (
    "reboot", "shutdown", "remove", "decommission",
    "restart", "start", "stop", "reload", "upgrade",
)
_T2_PROD_EXECUTE = ("query", "fetch_log", "verify")
_T2_PROD_APPROVE = ("apply_config", "backup", "transfer_file", "enable", "disable")
_T2_PROD_REQUIRED = (
    "restart", "start", "stop", "reload", "upgrade",
    "install", "scale", "run_script", "deploy", "rollback", "restore",
    "reboot", "shutdown", "remove", "decommission",
)

TEMPLATE_NAMES = ("template1", "template2", "template3", "template4")

# 模板展示名（CLI/UI 用）。
TEMPLATE_LABELS = {
    "template1": "单人本地项目",
    "template2": "小团队（local/test/dev/prod）",
    "template3": "中型团队（local/test/uat/dev/prod）",
    "template4": "自定义（级联多选）",
}


class MatrixValidationError(ValueError):
    """矩阵校验失败（结构/枚举/无 deny 语义）。errors 为逐条中文错误。"""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("；".join(errors))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def matrix_path(home: Optional[Path] = None) -> Path:
    """矩阵文件路径（<数据根>/matrix.yaml，随 VIGIL_HOME/profile 语义）。"""
    from hermes_constants import get_hermes_home
    base = Path(home) if home is not None else Path(get_hermes_home())
    return base.expanduser() / MATRIX_FILENAME


def actions(home: Optional[Path] = None) -> List[str]:
    """动作词表：优先读 schemas.yaml（P1 配置化源），读不到回退硬编码 23 个。"""
    try:
        from tools.topo_schemas import schema_list
        value = schema_list("actions", home)
        if isinstance(value, list) and value:
            return [str(a) for a in value]
    except Exception:
        pass
    return list(_FALLBACK_ACTIONS)


def _normalize_level(value: Any, where: str) -> str:
    """把文件里的档位值规范化为内部字符串（execute/approve/required）。

    - 字符串 execute/approve → 原样；deny 语义值 → 报错（设计铁律）；
    - dict 仅接受 ``{approve: required}``（其余 dict 形态拒绝）；
    - 其他类型 → 报错并列出合法档位。
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _DENY_VALUES:
            raise MatrixValidationError([
                f"{where}: 值 {value!r} 是 deny 语义——设计铁律（yapl-design.md §11.2）："
                "矩阵无 deny（deny 诱发 LLM 绕行，runbook 是预审流程，deny 在生成时拦）。"
                "请改用 execute / approve / {approve: required}。"
            ])
        if text == LEVEL_EXECUTE:
            return LEVEL_EXECUTE
        if text == LEVEL_APPROVE:
            return LEVEL_APPROVE
        raise MatrixValidationError([
            f"{where}: 档位 {value!r} 非法——合法值：execute / approve / "
            "{approve: required}（矩阵无 deny）。"
        ])
    if isinstance(value, dict):
        if list(value.keys()) == ["approve"] and value.get("approve") == "required":
            return LEVEL_REQUIRED
        raise MatrixValidationError([
            f"{where}: dict 形态 {value!r} 非法——仅接受 {{approve: required}}"
            "（强制人工）；其余 dict 形态拒绝。"
        ])
    raise MatrixValidationError([
        f"{where}: 档位 {value!r} 非法——合法值：execute / approve / {{approve: required}}。"
    ])


def _serialize_level(level: str) -> Any:
    """内部规范值 → 文件形态（execute/approve 字符串；required 写 {approve: required}）。"""
    if level == LEVEL_REQUIRED:
        return {"approve": "required"}
    return level


def load_matrix(home: Optional[Path] = None) -> Dict[str, Any]:
    """加载并校验 matrix.yaml（热生效——每次调用实时读文件，不缓存）。

    Returns:
        ``{"schema_version", "updated_at", "source", "base_template", "matrix",
        "sources", "warnings", "path"}``；matrix/sources 为 {env: {action: 值}}。

    Raises:
        MatrixValidationError: 结构/枚举/无 deny 语义违规（逐条中文错误）。
        FileNotFoundError: 文件不存在（调用方按需用 load_matrix_or_empty）。
    """
    path = matrix_path(home)
    if not path.is_file():
        raise FileNotFoundError(f"矩阵文件不存在: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MatrixValidationError([f"matrix.yaml 解析失败: {exc}"])
    return validate_matrix(raw, path)


def validate_matrix(raw: Any, path: Optional[Path] = None) -> Dict[str, Any]:
    """校验任意矩阵 YAML 结构，返回规范化的加载结果。

    校验全开（悲观——默认 LLM 是傻子，校验是合同执行力）：
    - 结构：顶层 dict、matrix 段存在、env 键非空、action 键 ∈ 动作词表
      （不在 = 报错并列出合法动作）、档位枚举合法；
    - 无 deny 语义：出现 deny/denied 等值 = 报错；
    - 漏配 action×env → 默认 approve（保守）+ warning 列出。
    """
    errors: List[str] = []
    warnings: List[str] = []

    if raw is None or not isinstance(raw, dict):
        raise MatrixValidationError(["matrix.yaml 必须是对象（schema_version/matrix 段）。"])

    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        errors.append(
            f"schema_version 必须是整数（当前 {schema_version!r}）；"
            "文件结构见 yapl-design.md §11.2。"
        )
    elif schema_version != SCHEMA_VERSION:
        warnings.append(
            f"schema_version={schema_version} 与当前支持版本 {SCHEMA_VERSION} 不同，"
            "按当前结构解析（不兼容字段可能被忽略）。"
        )

    top_source = str(raw.get("source") or "").strip() or ""
    base_template = str(raw.get("base_template") or "").strip() or ""
    if base_template and base_template not in TEMPLATE_NAMES:
        warnings.append(f"base_template 未知: {base_template!r}（合法：{', '.join(TEMPLATE_NAMES)}）。")

    matrix_raw = raw.get("matrix")
    if not isinstance(matrix_raw, dict) or not matrix_raw:
        errors.append("matrix 段缺失或为空——必须定义至少一个环境的档位（结构见 §11.2）。")
        matrix_raw = {}

    action_list = actions()
    action_set = set(action_list)
    sources_raw = raw.get("sources") or {}
    if not isinstance(sources_raw, dict):
        errors.append("sources 段必须是对象（{env: {action: 模板名 or manual}}）。")
        sources_raw = {}

    matrix: Dict[str, Dict[str, str]] = {}
    sources: Dict[str, Dict[str, str]] = {}
    for env_name, env_raw in matrix_raw.items():
        env = str(env_name).strip()
        if not env:
            errors.append("matrix 存在空环境名——环境名必须是非空字符串。")
            continue
        if not isinstance(env_raw, dict):
            errors.append(f"matrix.{env}: 必须是对象（{env} 下的 action → 档位）。")
            continue
        matrix[env] = {}
        sources[env] = {}
        for act_name, level_raw in env_raw.items():
            act = str(act_name).strip()
            if not act:
                errors.append(f"matrix.{env}: 存在空动作键。")
                continue
            if act not in action_set:
                errors.append(
                    f"matrix.{env}.{act}: 动作不在词表——合法动作：{', '.join(action_list)}"
                    "（schemas.yaml actions，§10.2）。"
                )
                continue
            try:
                level = _normalize_level(level_raw, f"matrix.{env}.{act}")
            except MatrixValidationError as exc:
                errors.extend(exc.errors)
                continue
            matrix[env][act] = level
            cell_source = ""
            if isinstance(sources_raw.get(env), dict):
                cell_source = str(sources_raw[env].get(act) or "").strip()
            if not cell_source:
                cell_source = top_source or base_template or "unknown"
            if cell_source not in ("manual",) and cell_source not in TEMPLATE_NAMES:
                warnings.append(f"sources.{env}.{act}: 来源 {cell_source!r} 未知，按原样保留。")
            sources[env][act] = cell_source

        # 漏配检查：该环境缺的动作 → 默认 approve（保守）+ warning。
        missing = sorted(action_set - set(matrix[env].keys()))
        if missing:
            shown = ", ".join(missing)
            warnings.append(
                f"matrix.{env} 漏配 {len(missing)} 个动作（{shown}）——默认 approve（保守）。"
            )

    if errors:
        raise MatrixValidationError(errors)

    result: Dict[str, Any] = {
        "schema_version": schema_version if isinstance(schema_version, int) else SCHEMA_VERSION,
        "updated_at": str(raw.get("updated_at") or _now_iso()),
        "source": top_source or "unknown",
        "base_template": base_template or "",
        "matrix": matrix,
        "sources": sources,
        "warnings": warnings,
        "path": str(path) if path is not None else str(matrix_path()),
    }
    return result


def load_matrix_or_empty(home: Optional[Path] = None) -> Dict[str, Any]:
    """热生效读路径：文件缺失 → 空矩阵（全部动作默认 approve 保守）+ warning。

    任何读取端（matrix_query / CLI show / UI GET）都走这里——不缓存、不预热，
    改完文件即生效（与 runbook 触发上下文同一读路径）。
    """
    path = matrix_path(home)
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": "",
            "source": "",
            "base_template": "",
            "matrix": {},
            "sources": {},
            "warnings": [f"矩阵文件不存在: {path}——全部动作默认 approve（保守）。"],
            "path": str(path),
        }
    return load_matrix(home)


def get_level(data: Dict[str, Any], env: str, action: str) -> Dict[str, Any]:
    """查 action × env 档位（matrix_query 与资产审批共用）。

    Returns ``{"level", "source", "configured"}``；查不到（env 或 action 未配）
    → level=approve（保守）+ source="" + configured=False。热生效：data 由调用
    方每次实时 load。
    """
    env = str(env or "").strip()
    action = str(action or "").strip()
    env_cfg = (data.get("matrix") or {}).get(env)
    if isinstance(env_cfg, dict) and action in env_cfg:
        return {
            "level": env_cfg[action],
            "source": ((data.get("sources") or {}).get(env) or {}).get(action, ""),
            "configured": True,
        }
    return {"level": LEVEL_APPROVE, "source": "", "configured": False}


# ---------------------------------------------------------------------------
# 模板生成（§11.3 严格度阶梯）
# ---------------------------------------------------------------------------

def _build_cells(actions_: List[str], *, execute: set, approve: set,
                 required: set) -> Dict[str, str]:
    """按档位集合构造单环境 {action: level}。缺集合覆盖的兜底 execute。"""
    cells: Dict[str, str] = {}
    for act in actions_:
        if act == "runbook":
            # YAPL 主框架阶段 C（OPS-DELTA #88）：runbook 第 24 动作不进入矩阵
            # setup 四模板——子 runbook 引用步骤查 runbook 动作档位，matrix.yaml
            # 未配 = 漏配默认 approve（保守，yapl-design.md §13.6）；手动
            # ``vigil matrix set runbook <env> <level>`` 仍可显式配置档位。
            continue
        if act in required:
            cells[act] = LEVEL_REQUIRED
        elif act in approve:
            cells[act] = LEVEL_APPROVE
        else:
            cells[act] = LEVEL_EXECUTE
    return cells


def _template1_cells(actions_: List[str]) -> Dict[str, str]:
    """模板 1 单人本地项目（1 环境 local）：execute 全部；approve 仅高危 4。"""
    return _build_cells(
        actions_,
        execute=set(actions_),
        approve=set(_HIGH_RISK_ACTIONS),
        required=set(),
    )


def _template2_cells(actions_: List[str], env: str) -> Dict[str, str]:
    """模板 2 小团队（local/test/dev/prod）单环境档位。"""
    if env == "local":
        return _template1_cells(actions_)
    if env == "test":
        return _template2_cells(actions_, "dev")
    if env == "dev":
        return _build_cells(
            actions_,
            execute=set(actions_) - set(_T2_DEV_APPROVE),
            approve=set(_T2_DEV_APPROVE),
            required=set(),
        )
    if env == "prod":
        return _build_cells(
            actions_,
            execute=set(_T2_PROD_EXECUTE),
            approve=set(_T2_PROD_APPROVE),
            required=set(_T2_PROD_REQUIRED),
        )
    raise ValueError(f"模板 2 只支持 local/test/dev/prod，收到 {env!r}")


def _template3_cells(actions_: List[str], env: str) -> Dict[str, str]:
    """模板 3 中型团队（local/test/uat/dev/prod）：test/uat=模板2dev、dev=模板2prod、
    prod 最高限制（execute 仅查询 3，其余全部强制人工）。"""
    if env == "local":
        return _template1_cells(actions_)
    if env == "test":
        return _template2_cells(actions_, "dev")
    if env == "uat":
        return _template2_cells(actions_, "dev")
    if env == "dev":
        return _template2_cells(actions_, "prod")
    if env == "prod":
        return _build_cells(
            actions_,
            execute=set(_T2_PROD_EXECUTE),
            approve=set(),
            required=set(actions_) - set(_T2_PROD_EXECUTE),
        )
    raise ValueError(f"模板 3 只支持 local/test/uat/dev/prod，收到 {env!r}")


_TEMPLATE_ENVS = {
    "template1": ("local",),
    "template2": ("local", "test", "dev", "prod"),
    "template3": ("local", "test", "uat", "dev", "prod"),
}


def template_cells(template: str, env: str, actions_: Optional[List[str]] = None) -> Dict[str, str]:
    """模板 × 环境 → {action: level}（静态定义，§11.3 逐字）。"""
    acts = actions_ if actions_ is not None else actions()
    if template == "template1":
        return _template1_cells(acts)
    if template == "template2":
        return _template2_cells(acts, env)
    if template == "template3":
        return _template3_cells(acts, env)
    raise ValueError(f"{template!r} 不支持按环境取格（模板 4 是自定义级联，用 init_matrix 传选择）。")


def template_matrix(template: str, actions_: Optional[List[str]] = None,
                    selections: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """生成模板矩阵（不含时间戳/来源——由 init_matrix 封装落盘）。

    Args:
        template: template1/2/3/4。
        selections: 模板 4 专用——{execute: [动作], approve: [动作]}；approve 集
            从 execute 剩余中选（级联语义：已选的从后续选项移除），剩余全部
            {approve: required}。
    """
    acts = actions_ if actions_ is not None else actions()
    if template not in TEMPLATE_NAMES:
        raise ValueError(f"未知模板 {template!r}——合法：{', '.join(TEMPLATE_NAMES)}")
    if template == "template4":
        if not selections:
            raise ValueError("模板 4（自定义）需要选择 execute 集与 approve 集（selections）。")
        execute_set = {str(a) for a in (selections.get("execute") or [])}
        approve_set = {str(a) for a in (selections.get("approve") or [])}
        bad = (execute_set | approve_set) - set(acts)
        if bad:
            raise ValueError(
                f"模板 4 选择的动作不在词表：{', '.join(sorted(bad))}——"
                f"合法动作：{', '.join(acts)}"
            )
        if execute_set & approve_set:
            raise ValueError(
                "模板 4 级联语义：execute 集与 approve 集不得重叠"
                "（已选的从后续选项移除）。"
            )
        matrix: Dict[str, Dict[str, str]] = {}
        for env in ("local", "test", "dev", "prod"):
            matrix[env] = _build_cells(
                acts, execute=execute_set, approve=approve_set,
                required=set(acts) - execute_set - approve_set,
            )
        return {"matrix": matrix, "envs": ["local", "test", "dev", "prod"]}

    envs = _TEMPLATE_ENVS[template]
    matrix = {env: template_cells(template, env, acts) for env in envs}
    return {"matrix": matrix, "envs": list(envs)}


def init_matrix(template: str, home: Optional[Path] = None,
                actions_: Optional[List[str]] = None,
                selections: Optional[Dict[str, List[str]]] = None,
                *, force: bool = False) -> Dict[str, Any]:
    """生成并落盘矩阵（setup 入口：CLI ``vigil matrix init`` / UI 模板选择）。

    已存在且非 force → 报错（安全资产防误覆盖；重建走 reset）。
    落盘记 source: 模板名（每格来源追踪的基底）。返回 load_matrix 结果。
    """
    path = matrix_path(home)
    if path.is_file() and not force:
        raise FileExistsError(
            f"矩阵文件已存在: {path}——安全资产防误覆盖；如要重建请先 "
            "``vigil matrix reset`` 或删除该文件。"
        )
    generated = template_matrix(template, actions_=actions_, selections=selections)
    data = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": template,
        "base_template": template,
        "matrix": generated["matrix"],
        "sources": {
            env: {act: template for act in generated["matrix"][env]}
            for env in generated["matrix"]
        },
    }
    write_matrix(data, home)
    return load_matrix(home)


# ---------------------------------------------------------------------------
# 受控写接口（人工通道：CLI/UI；LLM 无 set 路径）
# ---------------------------------------------------------------------------

def set_level(data: Dict[str, Any], env: str, action: str, level: str,
              actions_: Optional[List[str]] = None,
              *, source: str = "manual") -> Dict[str, Any]:
    """单格改档位（CLI ``vigil matrix set`` / UI PUT）。

    修改后：该格 sources 变 manual、顶层 source 变 manual（每格来源追踪）；
    updated_at 刷新。返回变更摘要 ``{"changed": bool, "old", "new"}``
    （调用方负责审计 record_event）。data 原地更新并返回。
    """
    env = str(env or "").strip()
    action = str(action or "").strip()
    acts = actions_ if actions_ is not None else actions()
    if not env:
        raise ValueError("env 必填且不能为空。")
    if action not in set(acts):
        raise ValueError(
            f"动作 {action!r} 不在词表——合法动作：{', '.join(acts)}（schemas.yaml actions）。"
        )
    if level not in _LEVELS:
        raise ValueError(
            f"档位 {level!r} 非法——合法值：execute / approve / required（矩阵无 deny）。"
        )
    if source not in ("manual", "template1", "template2", "template3", "template4"):
        raise ValueError(f"来源 {source!r} 非法（manual 或模板名）。")

    matrix = data.setdefault("matrix", {})
    sources = data.setdefault("sources", {})
    env_cfg = matrix.get(env)
    src_cfg = sources.get(env)
    old = (env_cfg or {}).get(action)
    if (old == level and src_cfg and src_cfg.get(action) == source
            and data.get("source") == source):
        return {"changed": False, "old": old, "new": level}

    env_cfg = matrix.setdefault(env, {})
    src_cfg = sources.setdefault(env, {})
    env_cfg[action] = level
    src_cfg[action] = source
    data["source"] = source if source != "manual" else "manual"
    data["updated_at"] = _now_iso()
    return {"changed": True, "old": old, "new": level}


def mark_all_manual(data: Dict[str, Any], actions_: Optional[List[str]] = None) -> int:
    """批量编辑（CLI edit / UI 全量保存）后：变动的格子来源 → manual。

    对比 data 与现有文件 diff；逐格变化标记 manual；新增动作/环境同样 manual。
    返回变动格子数。data 原地更新（调用方负责审计与落盘）。
    """
    changed = 0
    for env, env_cfg in (data.get("matrix") or {}).items():
        src_cfg = (data.get("sources") or {}).setdefault(env, {})
        for act in env_cfg:
            if src_cfg.get(act) not in ("manual",):
                src_cfg[act] = "manual"
                changed += 1
    if changed:
        data["source"] = "manual"
        data["updated_at"] = _now_iso()
    return changed


def reset_to_template(data: Dict[str, Any], home: Optional[Path] = None,
                      actions_: Optional[List[str]] = None,
                      template: Optional[str] = None) -> Dict[str, Any]:
    """回退到基底模板（CLI ``vigil matrix reset``）：放弃全部手动改动。

    template 省略时用 data["base_template"]（生成时的基底）。重置后 sources
    全部回模板名、顶层 source 回模板名。data 原地更新并返回。
    """
    base = template or str(data.get("base_template") or "").strip()
    if not base or base not in TEMPLATE_NAMES:
        raise ValueError(
            f"无法确定回退模板（base_template={base!r}）——请显式指定 --template。"
        )
    if base == "template4":
        raise ValueError(
            "模板 4 是自定义级联选择，无静态定义可回退——请重新 init 或逐格 set。"
        )
    generated = template_matrix(base, actions_=actions_)
    data["matrix"] = generated["matrix"]
    data["sources"] = {
        env: {act: base for act in generated["matrix"][env]}
        for env in generated["matrix"]
    }
    data["source"] = base
    data["base_template"] = base
    data["updated_at"] = _now_iso()
    return data


def write_matrix(data: Dict[str, Any], home: Optional[Path] = None) -> Path:
    """原子落盘 matrix.yaml（先写临时文件再 os.replace，防半写）。"""
    path = matrix_path(home)
    doc = {
        "schema_version": data.get("schema_version", SCHEMA_VERSION),
        "updated_at": data.get("updated_at") or _now_iso(),
        "source": data.get("source") or "",
        "base_template": data.get("base_template") or "",
        "matrix": {
            env: {act: _serialize_level(level) for act, level in env_cfg.items()}
            for env, env_cfg in (data.get("matrix") or {}).items()
        },
        "sources": data.get("sources") or {},
    }
    # 落盘前对**序列化后的文档**全量校验（校验器兜底：默认 LLM 是傻子）。
    # 校验对象是文件形态（required 写 {approve: required} dict），不是内存内部
    # 形态（"required" 字符串）——防止写入格式违规内容。
    validate_matrix(doc, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    fd, tmp = tempfile.mkstemp(prefix=".matrix-", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# CLI edit 支持（$EDITOR 打开 + 保存时校验）
# ---------------------------------------------------------------------------

def render_yaml(data: Dict[str, Any]) -> str:
    """把加载结果渲染为可编辑 YAML（edit 命令的编辑器内容）。"""
    doc = {
        "schema_version": data.get("schema_version", SCHEMA_VERSION),
        "updated_at": data.get("updated_at") or "",
        "source": data.get("source") or "",
        "base_template": data.get("base_template") or "",
        "matrix": {
            env: {act: _serialize_level(level) for act, level in env_cfg.items()}
            for env, env_cfg in (data.get("matrix") or {}).items()
        },
        "sources": data.get("sources") or {},
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _pick_editor() -> Optional[str]:
    editor = os.getenv("EDITOR") or os.getenv("VISUAL")
    if editor:
        return editor
    import shutil
    for cmd in ("nano", "vim", "vi", "code", "notepad"):
        if shutil.which(cmd):
            return cmd
    return None


def edit_matrix(home: Optional[Path] = None, *,
                editor: Optional[str] = None,
                content: Optional[str] = None) -> Dict[str, Any]:
    """``vigil matrix edit``：$EDITOR 打开当前矩阵，保存时校验（结构/枚举/
    无 deny 语义）后落盘，变动的格子来源 → manual。

    content 注入（测试用）：跳过编辑器直接校验并落盘给定 YAML 文本。
    Returns 变更摘要 {changed_cells, warnings, errors}；errors 非空 = 未落盘。
    """
    path = matrix_path(home)
    if not path.is_file():
        return {"changed_cells": 0, "warnings": [], "errors": [
            f"矩阵文件不存在: {path}——先运行 ``vigil matrix init --template N``。"
        ]}
    before = load_matrix(home)
    if content is None:
        editor_cmd = editor or _pick_editor()
        if not editor_cmd:
            return {"changed_cells": 0, "warnings": [], "errors": [
                f"未找到编辑器（$EDITOR 未设置）。矩阵文件在: {path}"
            ]}
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", prefix=".matrix-edit-", delete=False,
            encoding="utf-8",
        ) as tf:
            tf.write(render_yaml(before))
            tmp_path = tf.name
        try:
            proc = subprocess.run(
                shlex.split(editor_cmd) + [tmp_path],
                check=False,
            )
            if proc.returncode != 0:
                return {"changed_cells": 0, "warnings": [], "errors": [
                    f"编辑器退出码 {proc.returncode}——未落盘，矩阵保持不变。"
                ]}
            content = Path(tmp_path).read_text(encoding="utf-8")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    else:
        tmp_path = None

    try:
        raw = yaml.safe_load(content or "")
    except Exception as exc:
        return {"changed_cells": 0, "warnings": [], "errors": [f"YAML 解析失败: {exc}——未落盘。"]}
    try:
        after = validate_matrix(raw, path)
    except MatrixValidationError as exc:
        return {"changed_cells": 0, "warnings": [], "errors": list(exc.errors)}

    # diff：变动的格子（新增/删除/改档位）来源 → manual；顶层 source → manual。
    changed_cells = 0
    for env, env_cfg in (after["matrix"] or {}).items():
        src_cfg = after["sources"].setdefault(env, {})
        old_cfg = (before["matrix"] or {}).get(env, {})
        for act, level in env_cfg.items():
            if old_cfg.get(act) != level:
                src_cfg[act] = "manual"
                changed_cells += 1
            elif not src_cfg.get(act):
                src_cfg[act] = before.get("source") or after.get("base_template") or "unknown"
    for env, env_cfg in (before["matrix"] or {}).items():
        for act in env_cfg:
            if act not in (after["matrix"] or {}).get(env, {}):
                changed_cells += 1
    after["base_template"] = before.get("base_template") or after.get("base_template") or ""
    if changed_cells:
        after["source"] = "manual"
        after["updated_at"] = _now_iso()
    write_matrix(after, home)
    return {"changed_cells": changed_cells, "warnings": after["warnings"], "errors": []}
