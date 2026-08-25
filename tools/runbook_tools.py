"""Runbook (程序层) tools for the Ops Agent Harness.

Implements the program-layer contract from ``ops-agent-harness.md`` §1 and
§3 L4:

  runbooks/<name>.yaml      — structured YAML: 触发条件 + 步骤 + 命令 + 回滚
                              (incident runbooks) and, for ``kind: deploy``
                              with ``checklist: true``, an L4 deployment
                              checklist (前置核对 → 滚动发布 → 真实验证 →
                              回滚预案) whose phase order is enforced by
                              ``runbook_checkpoint``.

Runbook 格式门控（OPS-DELTA 批次四十 §AU）：runbook 只支持 ``.yaml``
（schema v0.1）。``runbooks/`` 下的非 ``.yaml`` 文件（.md/.yml/其他）不会被
``runbook_load`` 加载——能力门控检查会对此发警告（不再静默忽略），写入端
（write_file 落盘到 runbooks/ 目录）也会拒绝非 ``.yaml`` 扩展名。

Runbooks are loaded on demand (alert/task-triggered), NOT injected into the
system prompt.  The tool only returns content and tracks checklist state —
commands are executed by the agent through the terminal tool, so every
command still passes through the permission matrix (ops-agent-harness.md §3).

Data contract (schema v0.1): see ops-agent-harness.md §1 / §3 L4.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_RUNBOOKS_DIRNAME = "runbooks"
_STATE_FILENAME = ".runbook-state.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_VALID_ENVS = {"local", "test", "dev", "prod"}
_VALID_STATUSES = {"pass", "fail"}
# OPS-DELTA #21：runbook commands 的 <vault:path/field> 凭据引用占位符。
# runbook_load 永远不返回明文——占位符描述化返回，明文只在执行时由 agent 从
# 保险箱/vault 读取并立即注入命令（命令串过 agent/redact.py 打码）。
_VAULT_REF_RE = re.compile(r"<vault:([A-Za-z0-9_./-]+)>")
# runbook_create 专有：名称严格 kebab-case（小写字母/数字 + 连字符），
# 防路径穿越（".." / 隐藏文件/点号下划线）与非法文件名写盘。
_CREATE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VALID_KINDS = {"deploy", "incident", "checklist"}


# ── YAPL P2（批次五十三）：runbook schema v0.2 ────────────────────────────────
# 设计单一事实来源 yapl-design.md §10。v0.1 用 steps[].commands（命令写死），
# v0.2 用 steps[].action（动作枚举 + params），命令彻底消失。
_V2_KINDS = {"incident", "deploy", "maintenance", "checklist"}
_EXPECT_CHANNELS = (
    "kubectl", "docker", "docker_compose", "systemctl", "pm2",
    "http", "port", "process",
)
_ON_FAILURE_VALUES = ("stop", "continue", "rollback")
# 只读动作：on_failure: continue 只允许出现在只读动作（§10.4 校验器拦变更动作）。
_READONLY_ACTIONS = frozenset({"query", "fetch_log", "verify"})
# 变量引用（§10.5）：{{ steps.<id>.params.<key...> }} / {{ steps.<id>.outputs.<key> }}
# / {{ trigger_context.<field> }}——来源 = 触发上下文 + 步骤输出，LLM 不用猜值。
_VAR_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)*)\s*\}\}")
_TRIGGER_CONTEXT_FIELDS = frozenset(
    {"source", "alertname", "severity", "startsAt", "triggered_at"}
)

# 动作参数契约（§10.2 括号内声明）：required = 必填参数（缺失即报错，
# target 缺失 = 报错）；typed = 类型/结构校验（其余参数仅要求出现）。
# 执行修饰符 batch/interval/timeout/force 通用可选（不逐一列举）。
_ACTION_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "start": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "stop": {"required": ["target"], "typed": {"target": "topo_ref", "force": "bool"}},
    "restart": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "reload": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "enable": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "disable": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "reboot": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "shutdown": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "deploy": {"required": ["target"],
               "typed": {"target": "topo_ref", "image": "str", "version": "str"}},
    "rollback": {"required": ["target"],
                 "typed": {"target": "topo_ref", "to": "str"}},
    "scale": {"required": ["target", "replicas"],
              "typed": {"target": "topo_ref", "replicas": "int"}},
    "decommission": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "backup": {"required": ["target", "dest"],
               "typed": {"target": "topo_ref", "dest": "str"}},
    "restore": {"required": ["target", "from"],
                "typed": {"target": "topo_ref", "from": "str"}},
    "apply_config": {"required": ["target", "changes"],
                     "typed": {"target": "topo_ref", "changes": "changes"}},
    "query": {"required": [],
              "typed": {"target": "topo_ref", "pattern": "str"}},
    "fetch_log": {"required": ["target"],
                  "typed": {"target": "topo_ref", "lines": "int",
                            "grep": "str", "since": "str"}},
    "verify": {"required": ["target"], "typed": {"target": "topo_ref"}},
    "transfer_file": {"required": ["source", "dest"],
                      "typed": {"source": "file_ref", "dest": "file_ref"}},
    "run_script": {"required": ["script"], "typed": {"script": "asset"}},
    "install": {"required": ["target", "package"],
                "typed": {"target": "topo_ref", "package": "str",
                          "version": "str", "repo": "str"}},
    "upgrade": {"required": ["target", "package"],
                "typed": {"target": "topo_ref", "package": "str",
                          "version": "str", "repo": "str"}},
    "remove": {"required": ["target", "package"],
               "typed": {"target": "topo_ref", "package": "str", "deps": "bool"}},
    # YAPL 主框架阶段 C（OPS-DELTA #88）：runbook 第 24 动作——嵌套引用
    # （params: {ref: <名>, type: runbook|tool}）。ref = 子 runbook 名 或
    # 已注册编译契约工具名；type 必填不猜（runbook|tool 二选一）。
    "runbook": {"required": ["ref", "type"],
                "typed": {"ref": "str", "type": "runbook_ref_type"}},
}

# 引用校验的类型兼容表：主机族动作目标必须是 host/host_group/cluster（不能是
# service）；包族动作目标必须是 host/host_group。其余动作四种类型皆可
# （执行器按 action × target 类型 × managed_by 分派，P4）。
_TARGET_TYPE_RESTRICTIONS: Dict[str, frozenset] = {
    "reboot": frozenset({"host", "host_group", "cluster"}),
    "shutdown": frozenset({"host", "host_group", "cluster"}),
    "install": frozenset({"host", "host_group"}),
    "upgrade": frozenset({"host", "host_group"}),
    "remove": frozenset({"host", "host_group"}),
}


def _normalize_runbook_env(env: Any) -> Optional[str]:
    """runbook env → 四值档位（local/test/dev/prod）；非法返回 None。

    四值直通；老值 uat→prod、staging→dev（复用 ops_permissions._map_env_tier，
    档位映射 + 每个名字只警告一次，不阻断读取）；ops 配置里显式声明的老自定义名
    按 isolation/role 档位接受。其余未声明名（如 sandbox）视为非法——不沿用
    _map_env_tier 的"名字推导默认 dev"兜底，避免把拼写错误静默接受成 dev。
    本函数只做校验/比较用，不改写 data["env"]（load 保留文件原值）。
    """
    lower = str(env or "").strip().lower()
    if lower in _VALID_ENVS:
        return lower
    from tools.ops_permissions import (
        _LEGACY_ENV_TIER_MAP,
        _map_env_tier,
        _raw_env_definition,
    )
    if lower in _LEGACY_ENV_TIER_MAP:
        return _map_env_tier(lower)
    try:
        if _raw_env_definition(lower) is not None:
            return _map_env_tier(lower)
    except Exception:
        return None
    return None
# 疑似明文凭据模式（runbook_create 校验用）：赋值式（password= / token: ...）
# 与 flag 式（--password / -p value）。命中即拒绝（fail-closed），提示改用
# <vault:path/field> 占位符；只判"疑似"，不漏明文，宁误报也提示。
_PLAINTEXT_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*(\S+)"
)
# -p 支持带空格（-p secret）与紧贴（-psecret / -pSECRET）两种形态；其余
# --password/--token/--api-key 也带 = 与空格双重形态。
# --password/--token/--api-key 支持空格与 = 双重形态；-p 支持空格（-p secret）、
# 紧贴（-psecret / -pSECRET）、等号（-p=secret）三种。校验阶段再按命令语义判定
# （_is_password_flag）是否真密码。
_PLAINTEXT_SECRET_FLAG_RE = re.compile(
    r"(?i)(--password|--passwd|--token|--api-key)(?:\s*[=\s]+\s*)(\S+)"
    r"|-(p)(?:\s*[=\s]*)(\S+)"
)
# curl/scp 类 `-u user:password`（§AH 同源明文凭据形态）。
_PLAINTEXT_CRED_FLAG_RE = re.compile(r"(?i)(-u|--user)\s+([^\s<]+:[^\s]+)")

_DEFAULT_LOAD_SCHEMA = {
    "name": "runbook_load",
    "description": (
        "加载运维 runbook（程序层：结构化 YAML，含触发条件 + 步骤 + 命令 + 回滚）。"
        "runbook 只支持 .yaml（schema v0.1），.md/其他格式不会被加载。"
        "按 runbook 名精确加载，或按触发关键字/症状模糊匹配（省略参数时列出全部）。"
        "L4 部署 checklist（kind=deploy, checklist=true）会附带当前阶段门状态。"
        "本工具只返回内容，不执行任何命令——步骤里的命令由你通过终端执行，"
        "逐条过权限矩阵（ops-agent-harness.md §3）。执行运维操作前，先 topo_query "
        "确认目标实体在拓扑表中的身份和环境；跨环境操作默认拒绝。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "runbook": {
                "type": "string",
                "description": "runbook 名（如 harbor-restart）。精确加载。",
            },
            "query": {
                "type": "string",
                "description": "按触发关键字/症状模糊匹配（如 'harbor 健康检查失败'、'发布 gateway-svc'）。",
            },
        },
        "required": [],
    },
}

_DEFAULT_CHECKPOINT_SCHEMA = {
    "name": "runbook_checkpoint",
    "description": (
        "维护 L4 部署 checklist（仅 checklist=true 的 deploy runbook）的阶段门："
        "记录某步骤 pass/fail；前置步骤未全部 pass 时拒绝推进，防止跳过"
        "前置核对直接发布。reset=true 开启新一轮部署（清空该 runbook 的阶段状态）。"
        "返回该 runbook 的 checklist 状态供审计。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "runbook": {
                "type": "string",
                "description": "runbook 名（必须 checklist=true）。",
            },
            "step_id": {
                "type": "string",
                "description": "要记录的步骤 id（如 preflight / deploy / verify）。",
            },
            "status": {
                "type": "string",
                "enum": ["pass", "fail"],
                "description": "该步骤结果：pass（通过）或 fail（未通过）。",
            },
            "evidence": {
                "type": "string",
                "description": "判定依据（如验证命令输出摘要），审计用。",
            },
            "reset": {
                "type": "boolean",
                "description": "true 时先清空该 runbook 的阶段状态（新一轮部署）。",
            },
        },
        "required": ["runbook", "step_id", "status"],
    },
}

_DEFAULT_CREATE_SCHEMA = {
    "name": "runbook_create",
    "description": (
        "创建/更新运维 runbook（程序层：结构化 YAML，runbooks/<name>.yaml）。"
        "双 schema：v0.2（唯一允许的新建格式，声明式动作）steps 用 action+params，命令彻底消失；"
        "v0.1（存量风格）steps 用 commands。新建 runbook 必须用 v0.2；v0.1 commands 格式"
        "会被拒绝，仅供 overwrite 存量文件。v0.2 语法：\n"
        "- 步骤 = {id, title, action, params, expect?, on_failure?}；action 必须是动作词表"
        "（start/stop/restart/reload/enable/disable/reboot/shutdown/deploy/rollback/"
        "scale/decommission/backup/restore/apply_config/query/fetch_log/verify/"
        "transfer_file/run_script/install/upgrade/remove），params 按动作契约填必填"
        "（如 restart {target: 拓扑实体}、scale {target, replicas}、backup {target, dest}、"
        "apply_config {target, changes: [{key, value}]}）；\n"
        "- 目标四维范围：env ⊇ clusters ⊇ host_groups ⊇ hosts（均可空=不限），target 引用"
        "拓扑表实体（先 topo_query 确认）；\n"
        "- expect = {target: 检查通道, contains/http_status/body_contains/exit_code}；"
        "on_failure = stop/continue(只读动作)/rollback/{rollback: 场景名}；\n"
        "- triggers 双形态（字符串或 {alertname, severity}），checklist 用 schedule"
        "（{cron, timezone}）替代 triggers；\n"
        "- 变量引用 {{ steps.<id>.params.<key> }} / {{ trigger_context.<字段> }}；\n"
        "- 无 permission 字段（权限=操作矩阵唯一裁决）；run_script.script 只引用资产。\n"
        "v0.2 创建/变更会触发资产审批（YAPL §11.4）：矩阵判 {approve: required} 高危"
        "动作的 runbook 强制人工审批；approvals.mode=smart 且全部动作矩阵判 execute 时"
        "自动批准；审批失败不落盘。v0.1（仅存量 overwrite）保留原路径。"
        "runbook 只支持 .yaml——.md/其他格式不会被加载。"
        "runbook 是 Vigil 程序层机制（触发条件 + 步骤 + 命令 + 回滚），不是 Markdown 文档："
        "runbook_load 可按名/触发词加载，runbook_checkpoint 可门控部署阶段。"
        "用户说'沉淀/记录/保存为 runbook'时应调用本工具。v0.1 命令一律拒绝明文"
        "密码/token——用 <vault:path/field> 占位符（执行时从保险箱读取注入）。同名已存在需 "
        "overwrite=true 才覆盖。"
        "v0.2 完整示例：{name: svc-restart, title: 重启服务, version: 2, kind: incident, "
        "env: prod, triggers: [\"svc down\"], clusters: [], host_groups: [], hosts: [], "
        "steps: [{id: s1, title: 重启, action: restart, params: {target: <拓扑实体>}, "
        "expect: {target: kubectl, body_contains: \"1/1 Running\"}}], "
        "rollback: [{name: rb, steps: [{id: r1, title: 回滚, action: rollback, "
        "params: {target: <拓扑实体>}}]}]}——target 必须是拓扑表实体名，先 topo_query 确认。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "runbook": {
                "type": "string",
                "description": "runbook 名（kebab-case，如 ansible-syntax-check）。",
            },
            "title": {
                "type": "string",
                "description": "标题。",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "触发关键字/症状（模糊匹配用，如 [\"修改 ansible\", \"syntax check\"]）。",
            },
            "summary": {
                "type": "string",
                "description": "一句话摘要（可选）。",
            },
            "steps": {
                "type": "array",
                "description": (
                    "步骤列表。v0.2：每步 {id, title, action, params, expect?, on_failure?}"
                    "（action ∈ 动作词表；params 按动作契约，如 restart {target}、"
                    "scale {target, replicas}、backup {target, dest}、apply_config "
                    "{target, changes: [{key, value}]}、fetch_log {target, lines?}、"
                    "transfer_file {source: {host?, path}, dest: {host?, path}}、"
                    "run_script {script, args?}）；v0.1：每步 {id, title, commands: [命令]}"
                    "；checklist 步骤可带 verify+expect（真实验证）。"
                ),
            },
            "rollback": {
                "type": "array",
                "description": "回滚（可选）。v0.2：场景数组 [{name, steps: [{action, params}]}]；"
                "v0.1：[{title, commands}]。on_failure: rollback 指向场景名。",
            },
            "env": {
                "type": "string",
                "description": "适用环境 local/test/dev/prod（可选，默认不限制；老值 uat/staging 按档位映射）。",
            },
            "kind": {
                "type": "string",
                "enum": ["deploy", "incident", "checklist", "maintenance"],
                "description": "deploy/incident/checklist/maintenance（v0.2 加 maintenance；v0.1 不支持 maintenance，默认 incident）。",
            },
            "clusters": {
                "type": "array",
                "items": {"type": "string"},
                "description": "目标集群（v0.2；可空 = env 内 all；必须存在于拓扑表）。",
            },
            "host_groups": {
                "type": "array",
                "items": {"type": "string"},
                "description": "目标主机组（v0.2；可空 = 不限组；host_group 不跨集群）。",
            },
            "hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "目标主机（v0.2；可空 = 不限单机；必须存在于拓扑表）。",
            },
            "schedule": {
                "type": "object",
                "description": "定时（v0.2，checklist 用，替代 triggers）：{cron: 5/6 段表达式, timezone: IANA 名}；"
                "cron 由 cron 工具生成，禁止手算。",
            },
            "on_failure": {
                "type": ["string", "object"],
                "description": "runbook 级默认失败处理（v0.2）：stop/continue/rollback/{rollback: 场景名}。",
            },
            "overwrite": {
                "type": "boolean",
                "description": "同名已存在时需 true 才覆盖（默认 false）。",
            },
        },
        "required": ["runbook", "title", "steps"],
    },
}


# ---------------------------------------------------------------------------
# Loaders / validation
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _runbook_path(home: Path, name: str) -> Optional[Path]:
    """Validate runbook name and resolve <home>/runbooks/<name>.yaml."""
    if not name or not isinstance(name, str) or not _NAME_RE.match(name):
        return None
    return (_runbooks_dir(home) / f"{name}.yaml").resolve()


def _state_path(home: Path) -> Path:
    return _runbooks_dir(home) / _STATE_FILENAME


def _runbooks_dir(home: Path) -> Path:
    """runbooks 数据目录：数据只挂在解析出的 home 下（home/runbooks）。

    OPS-DELTA #14 的 sibling ops profile 回退已删除——不再回退
    <root>/profiles/ops/runbooks，无数据即报"数据缺失"。
    """
    from tools.ops_data_home import resolve_ops_data_home
    return resolve_ops_data_home(home, _RUNBOOKS_DIRNAME) / _RUNBOOKS_DIRNAME


def _normalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj


def _is_checklist_runbook(data: Dict[str, Any]) -> bool:
    """runbook 是否按 L4 部署 checklist 处理。

    OPS-DELTA #32：``kind: deploy`` 的 runbook 默认 ``checklist: true``
    （部署阶段门强制，任何触发部署的路径必须过阶段门）；显式
    ``checklist: false`` 仍可关闭。
    """
    if "checklist" in data:
        return bool(data.get("checklist"))
    return data.get("kind") == "deploy"


def _load_runbook(home: Path, name: str) -> Optional[Dict[str, Any]]:
    path = _runbook_path(home, name)
    if path is None or not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("runbook: failed to parse %s: %s", path, exc)
        raise ValueError(f"runbook YAML 解析失败: {name} ({exc})")
    if not isinstance(data, dict):
        raise ValueError(f"runbook 顶层必须是对象: {name}")
    return _normalize(data)


# ---------------------------------------------------------------------------
# v0.2 分层校验器（YAPL P2：结构 / 引用 / 关系三层，默认全开悲观）
# ---------------------------------------------------------------------------

def _is_v2_runbook(data: Dict[str, Any]) -> bool:
    """Schema 分流：步骤含 ``action`` → v0.2；含 ``commands`` → v0.1。

    显式 ``version: 2`` 也判 v0.2。混合两种风格 → 直接报错（一次 runbook 只
    用一种风格）。v0.1 的 checklist 步骤（verify+expect、无 commands）无
    action，仍走 v0.1。
    """
    steps = data.get("steps") or []
    if not isinstance(steps, list):
        return data.get("version") == 2
    has_action = any(isinstance(s, dict) and "action" in s for s in steps)
    has_commands = any(isinstance(s, dict) and "commands" in s for s in steps)
    if has_action and has_commands:
        raise ValueError(
            "runbook 步骤不得混用 v0.1 commands 与 v0.2 action——一次 runbook "
            "只用一种风格（存量 v0.1 用 commands；v0.2 用 action+params）。"
        )
    if has_action:
        return True
    return data.get("version") == 2


def _v2_actions(home: Optional[Path]) -> Optional[List[str]]:
    """schemas.yaml actions 词表（动作未知不 unknown 兜底——动作是执行的）。"""
    try:
        from tools.topo_schemas import schema_list
        return schema_list("actions", home)
    except Exception:
        return None


def _topology_reference_index(home: Optional[Path]) -> Optional[Dict[str, str]]:
    """拓扑引用索引：{实体名: kind}（cluster/host_group/host/service）。

    引用校验（§10.8 第二层）：target 必须存在于拓扑表。读取失败/无拓扑 → None
    （引用层跳过，不阻断——结构/关系层照常）。
    """
    try:
        from tools.topo_tools import _all_services, load_topology
    except Exception:
        return None
    topo = load_topology(home)
    if topo is None:
        return None
    idx: Dict[str, str] = {}
    for cluster in topo.get("clusters") or []:
        if isinstance(cluster, dict) and cluster.get("name"):
            idx[str(cluster["name"])] = "cluster"
            for hg in cluster.get("host_groups") or []:
                if isinstance(hg, str) and hg.strip():
                    idx.setdefault(hg.strip(), "host_group")
    for host in topo.get("hosts") or []:
        if isinstance(host, dict) and host.get("name"):
            idx.setdefault(str(host["name"]), "host")
    for svc in _all_services(topo, home):
        if svc.get("name"):
            idx.setdefault(str(svc["name"]), "service")
    return idx or None


def _check_param_value(action: str, key: str, value: Any,
                       step_id: str, name: str) -> Optional[str]:
    """动作参数类型/结构校验；返回错误文案或 None（仅校验已声明类型的参数）。"""
    kind = (_ACTION_CONTRACTS.get(action) or {}).get("typed", {}).get(key)
    if kind is None:
        return None
    if kind == "str":
        if not isinstance(value, str):
            return f"runbook {name} 步骤 {step_id!r} 的 {action}.{key} 必须是字符串"
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"runbook {name} 步骤 {step_id!r} 的 {action}.{key} 必须是整数"
    elif kind == "bool":
        if not isinstance(value, bool):
            return f"runbook {name} 步骤 {step_id!r} 的 {action}.{key} 必须是 true/false"
    elif kind == "topo_ref":
        if not isinstance(value, str) or not value.strip():
            return f"runbook {name} 步骤 {step_id!r} 的 {action}.{key} 必须是拓扑实体引用（字符串）"
    elif kind == "asset":
        if not isinstance(value, str) or not value.strip():
            return f"runbook {name} 步骤 {step_id!r} 的 {action}.script 必须是资产引用"
        if re.search(r"[\s\n;#&|$<>`(){}\[\]]", value) or value.startswith("#!"):
            return (
                f"runbook {name} 步骤 {step_id!r} 的 run_script.script 只引用资产"
                "（不内联脚本）——拒绝换行/空白/shell 元字符；资产名形如 "
                "scripts/backup.sh 或 backup"
            )
    elif kind == "changes":
        if not isinstance(value, list) or not value:
            return f"runbook {name} 步骤 {step_id!r} 的 apply_config.changes 必须是非空列表"
        for i, ch in enumerate(value):
            if not isinstance(ch, dict) or not str(ch.get("key") or "").strip():
                return (
                    f"runbook {name} 步骤 {step_id!r} 的 apply_config.changes[{i}] 必须含"
                    " key（value 可选）"
                )
    elif kind == "runbook_ref_type":
        if value not in ("runbook", "tool"):
            return (
                f"runbook {name} 步骤 {step_id!r} 的 runbook.type 必须是 "
                f"runbook|tool 二选一（收到 {value!r}）——type 必填不猜："
                "type=runbook 引子 runbook，type=tool 引编译契约工具"
            )
    elif kind == "file_ref":
        if not isinstance(value, dict) or not str(value.get("path") or "").strip():
            return (
                f"runbook {name} 步骤 {step_id!r} 的 {action}.{key} 必须是 {{path: ...}}"
                "（host 可选，缺省 = 目标机）"
            )
        if "host" in value and not isinstance(value["host"], str):
            return f"runbook {name} 步骤 {step_id!r} 的 {action}.{key}.host 必须是字符串"
    return None


def _validate_expect(step: Dict[str, Any], name: str, where: str) -> None:
    expect = step.get("expect")
    if expect is None:
        return
    if not isinstance(expect, dict):
        raise ValueError(f"runbook {name} {where} 的 expect 必须是对象")
    channel = expect.get("target")
    if channel not in _EXPECT_CHANNELS:
        raise ValueError(
            f"runbook {name} {where} 的 expect.target 必须是检查通道之一 "
            f"{list(_EXPECT_CHANNELS)}（如 http/kubectl/systemctl），收到 {channel!r}"
        )
    preds = {k: v for k, v in expect.items() if k in
             ("contains", "http_status", "body_contains", "exit_code")}
    if not preds:
        raise ValueError(
            f"runbook {name} {where} 的 expect 至少需要一个谓词"
            "（contains / http_status / body_contains / exit_code）"
        )
    if "contains" in preds and not isinstance(preds["contains"], dict):
        raise ValueError(f"runbook {name} {where} 的 expect.contains 必须是 key-value 对象")
    for pk in ("http_status", "exit_code"):
        if pk in preds and (isinstance(preds[pk], bool) or not isinstance(preds[pk], int)):
            raise ValueError(f"runbook {name} {where} 的 expect.{pk} 必须是整数")
    if "body_contains" in preds and not isinstance(preds["body_contains"], str):
        raise ValueError(f"runbook {name} {where} 的 expect.body_contains 必须是字符串")
    if channel == "http" and "url" in expect and not isinstance(expect["url"], str):
        raise ValueError(f"runbook {name} {where} 的 expect.url 必须是字符串")


def _validate_on_failure(value: Any, name: str, where: str,
                         rollback_names: List[str],
                         action: Optional[str] = None) -> None:
    """on_failure 三值语义 + 指定回滚场景引用（§10.4）。"""
    if value is None:
        return
    if isinstance(value, str):
        if value not in _ON_FAILURE_VALUES:
            raise ValueError(
                f"runbook {name} {where} 的 on_failure 必须是 {list(_ON_FAILURE_VALUES)} "
                "之一或 {rollback: <场景名>}，收到 {value!r}"
            )
        if value == "continue" and action is not None and action not in _READONLY_ACTIONS:
            raise ValueError(
                f"runbook {name} {where} 的 on_failure: continue 只允许只读动作"
                f"（query/fetch_log/verify），{action} 是变更动作——失败继续会掩盖问题，"
                "改用 stop 或 rollback"
            )
        if value == "rollback" and not rollback_names:
            raise ValueError(
                f"runbook {name} {where} 的 on_failure: rollback 需要 rollback 场景"
                "（至少一个）"
            )
        return
    if isinstance(value, dict) and set(value.keys()) == {"rollback"}:
        scenario = str(value.get("rollback") or "").strip()
        if not scenario:
            raise ValueError(f"runbook {name} {where} 的 on_failure.rollback 场景名不能为空")
        if scenario not in rollback_names:
            raise ValueError(
                f"runbook {name} {where} 的 on_failure 引用不存在的回滚场景 {scenario!r}"
                f"（可用: {', '.join(rollback_names) or '无'}）"
            )
        return
    raise ValueError(
        f"runbook {name} {where} 的 on_failure 必须是 stop/continue/rollback "
        "或 {rollback: <场景名>}"
    )


def _validate_v2_step(step: Any, name: str, where: str, actions: List[str],
                      rollback_names: List[str],
                      runbook_on_failure: Any) -> None:
    if not isinstance(step, dict):
        raise ValueError(f"runbook {name} {where} 每项必须是对象")
    step_id = step.get("id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise ValueError(f"runbook {name} {where} 每项必须含非空字符串 id")
    if not isinstance(step.get("title"), str) or not step["title"].strip():
        raise ValueError(f"runbook {name} {where} 步骤 {step_id!r} 缺少非空 title")
    action = step.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError(
            f"runbook {name} {where} 步骤 {step_id!r} 缺少 action——v0.2 步骤必须声明"
            "动作枚举（如 restart/verify/backup），命令彻底消失"
        )
    if actions is not None and action not in actions:
        raise ValueError(
            f"runbook {name} {where} 步骤 {step_id!r} 的 action {action!r} 不在动作词表"
            f"（可用: {', '.join(actions)}；例: restart {{target: harbor}}）。"
            "动作未知无法执行，不兜底——改用词表内动作"
        )
    params = step.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"runbook {name} {where} 步骤 {step_id!r} 的 params 必须是对象")
    contract = _ACTION_CONTRACTS.get(action) or {}
    for req in contract.get("required") or []:
        if req not in params:
            raise ValueError(
                f"runbook {name} {where} 步骤 {step_id!r} 的 {action} 缺少必填参数 "
                f"{req!r}（契约: {action} {dict(contract.get('typed') or {})}）"
            )
    for key, value in params.items():
        err = _check_param_value(action, key, value, step_id, name)
        if err:
            raise ValueError(err)
    _validate_expect(step, name, f"步骤 {step_id!r}")
    effective = step.get("on_failure", runbook_on_failure)
    _validate_on_failure(effective, name, f"步骤 {step_id!r}", rollback_names, action)


def _validate_schedule(schedule: Any, name: str) -> None:
    if not isinstance(schedule, dict):
        raise ValueError(f"runbook {name} 的 schedule 必须是对象 {{cron, timezone}}")
    cron = schedule.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        raise ValueError(
            f"runbook {name} 的 schedule.cron 必填——cron 表达式（5/6 段）由 cron 工具"
            "生成，禁止 LLM 手算"
        )
    fields = cron.strip().split()
    if len(fields) not in (5, 6):
        raise ValueError(
            f"runbook {name} 的 schedule.cron {cron!r} 必须 5/6 段（分 时 日 月 周"
            "[秒]），收到 {len(fields)} 段——用 cron 工具生成，不要手算"
        )
    if any(not re.fullmatch(r"[0-9A-Za-z*?/,@#-]+", f) for f in fields):
        raise ValueError(f"runbook {name} 的 schedule.cron {cron!r} 含非法字符")
    tz = schedule.get("timezone")
    if not isinstance(tz, str) or not tz.strip():
        raise ValueError(
            f"runbook {name} 的 schedule.timezone 必填（IANA 时区名，如 Asia/Shanghai）"
        )
    try:
        ZoneInfo(tz.strip())
    except Exception:
        raise ValueError(
            f"runbook {name} 的 schedule.timezone {tz!r} 不是合法 IANA 时区名"
            "（如 Asia/Shanghai / UTC）"
        )


def _validate_triggers_v2(triggers: Any, name: str) -> None:
    """双形态触发：自然语言字符串 + 结构化对象（alertname 等精确匹配）。"""
    if not isinstance(triggers, list):
        raise ValueError(f"runbook {name} 的 triggers 必须是列表（字符串或对象）")
    for t in triggers:
        if isinstance(t, str):
            if not t.strip():
                raise ValueError(f"runbook {name} 的 triggers 含空字符串")
            continue
        if isinstance(t, dict):
            if not t or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool))
                                for k, v in t.items()):
                raise ValueError(
                    f"runbook {name} 的 triggers 结构化项必须是标量键值对象"
                    "（如 {alertname: HarborHealthcheckDown, severity: critical}）"
                )
            continue
        raise ValueError(f"runbook {name} 的 triggers 每项必须是字符串或对象")


def _iter_param_strings(params: Dict[str, Any]):
    """遍历 params 值中的字符串（含嵌套 dict/list），供变量引用解析。

    产出 (key, text)：key 为直接所属参数名（嵌套时沿用外层 key）。
    """
    if isinstance(params, dict):
        for k, v in params.items():
            for key, text in _iter_param_strings(v):
                yield (k, text) if not key else (key, text)
    elif isinstance(params, list):
        for v in params:
            yield from _iter_param_strings(v)
    elif isinstance(params, str):
        yield ("", params)


def _validate_v2_var_refs(data: Dict[str, Any], name: str) -> None:
    """关系层·变量引用：步骤存在 / params 字段存在 / 自引用拒绝。"""
    steps = data.get("steps") or []
    steps_by_id = {str(s.get("id")): s for s in steps if isinstance(s, dict)}
    rollback = data.get("rollback") or []
    owners: List[Dict[str, Any]] = []
    for s in steps:
        if isinstance(s, dict):
            owners.append(s)
    for scenario in rollback:
        if isinstance(scenario, dict):
            for s in scenario.get("steps") or []:
                if isinstance(s, dict):
                    owners.append(s)
    backup_ids = {
        str(s.get("id")) for s in steps
        if isinstance(s, dict) and s.get("action") == "backup"
    }
    for owner in owners:
        owner_id = str(owner.get("id") or "")
        action = str(owner.get("action") or "")
        for param_key, text in _iter_param_strings(owner.get("params") or {}):
            for match in _VAR_REF_RE.finditer(text):
                path = match.group(1)
                segs = path.split(".")
                if segs[0] == "trigger_context":
                    if len(segs) != 2:
                        raise ValueError(
                            f"runbook {name} 步骤 {owner_id!r} 的变量引用 "
                            f"{{{path}}} 不合法——trigger_context 引用形如 "
                            "{{ trigger_context.alertname }}"
                        )
                    if segs[1] not in _TRIGGER_CONTEXT_FIELDS:
                        raise ValueError(
                            f"runbook {name} 步骤 {owner_id!r} 的变量引用 "
                            f"{{{path}}} 的字段 {segs[1]!r} 不在触发上下文"
                            f"（可用: {sorted(_TRIGGER_CONTEXT_FIELDS)}）"
                        )
                    continue
                if len(segs) < 3 or segs[0] != "steps" or segs[2] not in ("params", "outputs"):
                    raise ValueError(
                        f"runbook {name} 步骤 {owner_id!r} 的变量引用 {{{path}}} 不合法"
                        "——必须是 {{ steps.<id>.params.<key...> }} / "
                        "{{ steps.<id>.outputs.<key> }} / {{ trigger_context.<field> }}"
                    )
                ref_id = segs[1]
                if ref_id == owner_id:
                    raise ValueError(
                        f"runbook {name} 步骤 {owner_id!r} 自引用变量 {{{path}}}——"
                        "步骤不能引用自己的 params"
                    )
                target = steps_by_id.get(ref_id)
                if target is None:
                    raise ValueError(
                        f"runbook {name} 步骤 {owner_id!r} 的变量引用 {{{path}}} 指向"
                        f"不存在的步骤 {ref_id!r}"
                    )
                if segs[2] == "params":
                    sub = (target.get("params") or {})
                    found = True
                    for key in segs[3:]:
                        if isinstance(sub, dict) and key in sub:
                            sub = sub[key]
                        else:
                            found = False
                            break
                    if not found or not segs[3:]:
                        raise ValueError(
                            f"runbook {name} 步骤 {owner_id!r} 的变量引用 {{{path}}} 的"
                            f"params 字段不存在于步骤 {ref_id!r}"
                        )
                    if action in ("restore", "rollback") and param_key == "from" \
                            and ref_id not in backup_ids:
                        raise ValueError(
                            f"runbook {name} 步骤 {owner_id!r} 的 {action}.from 必须引用"
                            "backup 步骤的 dest（{{ steps.<backup-id>.params.dest }}），"
                            f"步骤 {ref_id!r} 不是 backup 动作"
                        )
                # outputs 是执行期产物，静态校验只查步骤存在


def _runbook_names(home: Optional[Path]) -> Set[str]:
    """runbooks/*.yaml 文件 stem 集（嵌套引用校验的引用面）。"""
    try:
        rdir = Path(home) / "runbooks"
        if not rdir.is_dir():
            return set()
        return {p.stem for p in rdir.glob("*.yaml")
                if not p.name.startswith(".")}
    except Exception:
        return set()


def _compiled_contract_names(home: Optional[Path]) -> Set[str]:
    """contracts/registry.yaml 已注册编译契约名集（type=tool 引用的引用面）。"""
    try:
        from tools.contract_compile import registered_contracts
        return set((registered_contracts(home) or {}).keys())
    except Exception:
        return set()


def _find_runbook_cycle(name: str,
                        graph: Dict[str, List[str]]) -> Optional[List[str]]:
    """DFS 从 ``name`` 出发找它参与的引用环；返回环路路径 [a, b, a]。"""
    def dfs(node: str, path: List[str]):
        if node in path:
            i = path.index(node)
            return path[i:] + [node]
        nxts = graph.get(node) or []
        if not nxts:
            return None
        path.append(node)
        for nxt in nxts:
            found = dfs(nxt, path)
            if found:
                return found
        path.pop()
        return None
    return dfs(name, [])


def _validate_runbook_refs(data: Dict[str, Any], name: str,
                           home: Optional[Path]) -> None:
    """关系层·runbook 引用：ref 存在（type=runbook → runbooks/；type=tool →
    contracts/registry.yaml）+ 环状防护（引用自己/互相引用 → 拒绝，报环路路径）。

    变量不跨层语义：子 runbook 文件内 {{ steps... }} 只引用自身步骤（本校验器
    不跨文件解析）；父向子传值走 params 显式传入——父 steps 的 params 里的
    {{ }} 由父执行时解析后传入子（OPS-DELTA #88 注明）。
    """
    rbs = _runbook_names(home)
    tools = _compiled_contract_names(home)
    steps: List[Dict[str, Any]] = [s for s in (data.get("steps") or [])
                                   if isinstance(s, dict)]
    for scenario in (data.get("rollback") or []):
        if isinstance(scenario, dict):
            steps.extend(s for s in (scenario.get("steps") or [])
                         if isinstance(s, dict))
    edges: Dict[str, List[str]] = {}
    for s in steps:
        if s.get("action") != "runbook":
            continue
        params = s.get("params") or {}
        step_id = str(s.get("id") or "")
        ref = str(params.get("ref") or "").strip()
        rtype = str(params.get("type") or "").strip()
        if not ref or rtype not in ("runbook", "tool"):
            continue  # 结构层已拒绝（ref 必填 / type 枚举）
        if ref == name:
            raise ValueError(
                f"runbook {name} 步骤 {step_id!r} 引用自己（ref={ref!r}）——"
                "runbook 嵌套禁止引用自己（引用图无环）；请断开自引用"
            )
        if rtype == "runbook":
            if ref not in rbs:
                raise ValueError(
                    f"runbook {name} 步骤 {step_id!r} 引用的子 runbook {ref!r} "
                    "不存在——先 runbook_create 创建（runbooks/<name>.yaml）"
                )
            edges.setdefault(name, []).append(ref)
        else:
            if ref not in tools:
                raise ValueError(
                    f"runbook {name} 步骤 {step_id!r} 引用的编译契约工具 {ref!r} "
                    "未注册——先 vigil contract compile <name>（contracts/"
                    "registry.yaml 有记录才可引用；type=tool = run_script 资产"
                    "预审语义）"
                )
    if not edges:
        return
    # 全图边（含其他 runbook 的引用——跨文件互相引用成环也要拒）。
    graph = dict(edges)
    for rb in sorted(rbs):
        try:
            other = _load_runbook(home, rb)
        except Exception:
            continue
        if not isinstance(other, dict):
            continue
        for s in (other.get("steps") or []):
            if not isinstance(s, dict) or s.get("action") != "runbook":
                continue
            params = s.get("params") or {}
            if str(params.get("type") or "") == "runbook":
                r = str(params.get("ref") or "").strip()
                if r:
                    graph.setdefault(rb, []).append(r)
    cycle = _find_runbook_cycle(name, graph)
    if cycle:
        raise ValueError(
            f"runbook {name} 引用图成环（环路: {' → '.join(cycle)}）——"
            "runbook 嵌套禁止互相引用（引用图无环）；请断开环路（子 runbook "
            "只引用更细粒度的原子动作）"
        )


def _validate_runbook_v2(data: Dict[str, Any], name: str,
                         home: Optional[Path] = None) -> None:
    """v0.2 分层校验：结构 → 引用（拓扑）→ 关系（交叉引用），默认全开悲观。"""
    # ── 结构层 ──
    rb_name = data.get("name")
    if rb_name != name:
        raise ValueError(f"runbook 内 name({rb_name!r}) 与文件名({name!r})不一致")
    if not _CREATE_NAME_RE.match(name):
        raise ValueError(
            f"runbook {name} 名称非法——必须 kebab-case（小写字母/数字 + 连字符）"
        )
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError(f"runbook {name} 缺少 title")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"runbook {name} 的 version 必须是正整数（v0.2 写 2）")
    kind = data.get("kind")
    if kind not in _V2_KINDS:
        raise ValueError(
            f"runbook {name} 的 kind 必须是 {sorted(_V2_KINDS)} 之一，收到 {kind!r}"
        )
    env = data.get("env")
    if env is not None and _normalize_runbook_env(env) is None:
        raise ValueError(
            f"runbook {name} env 非法: {env!r}（local/test/dev/prod；老值 uat/staging 按档位映射）"
        )
    if "permission" in data:
        raise ValueError(
            f"runbook {name} 含 permission 字段——设计定案（yapl-design.md §10.10）：权限 = "
            "操作矩阵唯一裁决，runbook 无 permission 字段，请删除该字段"
        )
    for field in ("clusters", "host_groups", "hosts"):
        value = data.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(
                f"runbook {name} 的 {field} 必须是字符串数组（可空 = 不限）"
            )
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"runbook {name} 缺少非空 steps")
    actions = _v2_actions(home)
    rollback = data.get("rollback") or []
    if rollback is not None and not isinstance(rollback, list):
        raise ValueError(f"runbook {name} 的 rollback 必须是场景数组（[{name, steps}]）")
    rollback_names: List[str] = []
    if rollback:
        for scenario in rollback:
            if not isinstance(scenario, dict) or not str(scenario.get("name") or "").strip():
                raise ValueError(
                    f"runbook {name} 的 rollback 场景必须含 name（如 rollback-main）"
                )
            rname = str(scenario["name"]).strip()
            if rname in rollback_names:
                raise ValueError(f"runbook {name} 的 rollback 场景名重复: {rname!r}")
            rollback_names.append(rname)
            rsteps = scenario.get("steps")
            if not isinstance(rsteps, list) or not rsteps:
                raise ValueError(f"runbook {name} 的 rollback 场景 {rname!r} 缺少非空 steps")
            seen: set = set()
            for s in rsteps:
                if isinstance(s, dict) and isinstance(s.get("id"), str):
                    if s["id"] in seen:
                        raise ValueError(
                            f"runbook {name} 的 rollback 场景 {rname!r} 步骤 id 重复: {s['id']!r}"
                        )
                    seen.add(s["id"])
                _validate_v2_step(s, name, f"rollback 场景 {rname!r}", actions,
                                  rollback_names, "stop")
    runbook_on_failure = data.get("on_failure")
    _validate_on_failure(runbook_on_failure, name, "runbook 级", rollback_names)
    if "schedule" in data and "triggers" in data and data.get("triggers"):
        raise ValueError(
            f"runbook {name} 的 triggers 与 schedule 互斥——checklist 用 schedule 替代"
            " triggers（§10.1）"
        )
    if "triggers" in data and data.get("triggers") is not None:
        _validate_triggers_v2(data.get("triggers"), name)
    if "schedule" in data and data.get("schedule") is not None:
        _validate_schedule(data.get("schedule"), name)
    seen_ids: set = set()
    for s in steps:
        if isinstance(s, dict) and isinstance(s.get("id"), str):
            if s["id"] in seen_ids:
                raise ValueError(f"runbook {name} 步骤 id 重复: {s['id']!r}")
            seen_ids.add(s["id"])
        _validate_v2_step(s, name, "steps", actions, rollback_names, runbook_on_failure)

    # ── 引用层（拓扑表）──
    idx = _topology_reference_index(home)
    if idx is not None:
        topo = None
        try:
            from tools.topo_tools import load_topology
            topo = load_topology(home)
        except Exception:
            topo = None
        host_endpoints = set()
        for h in (topo or {}).get("hosts") or []:
            if isinstance(h, dict):
                ep = str(h.get("endpoint") or "").strip()
                if ep:
                    host_endpoints.add(ep.split(":")[0])
        cluster_names = {str(c.get("name")) for c in
                         ((topo or {}).get("clusters") or [])
                         if isinstance(c, dict) and c.get("name")}
        for cluster in data.get("clusters") or []:
            if idx.get(str(cluster)) != "cluster":
                raise ValueError(
                    f"runbook {name} 的目标集群 {cluster!r} 不在拓扑表"
                    f"（可用集群: {sorted(cluster_names) or '无'}）。先 topo_query 确认"
                    "或用拓扑表已有集群"
                )
        for hg in data.get("host_groups") or []:
            if idx.get(str(hg)) != "host_group":
                raise ValueError(
                    f"runbook {name} 的目标主机组 {hg!r} 不在拓扑表（host_group 属于某个"
                    "集群的 host_groups 数组）。先 topo_query 确认"
                )
            owner = _host_group_owner(home, str(hg))
            if data.get("clusters") and owner not in set(data.get("clusters") or []):
                raise ValueError(
                    f"runbook {name} 的主机组 {hg!r} 属于集群 {owner!r}，不在声明的"
                    f"clusters {data.get('clusters')} 内——四维严格嵌套"
                    "（host_group 不跨集群）"
                )
        for host in data.get("hosts") or []:
            kind_of = idx.get(str(host))
            if kind_of != "host":
                raise ValueError(
                    f"runbook {name} 的目标主机 {host!r} 不在拓扑表（hosts 段）。"
                    "先 topo_discover/topo_query 确认或用拓扑表已有主机"
                )
            owner_cluster = _host_cluster(home, str(host))
            if data.get("clusters") and owner_cluster not in set(data.get("clusters") or []):
                raise ValueError(
                    f"runbook {name} 的目标主机 {host!r} 属于集群 {owner_cluster!r}，不在"
                    f"声明的 clusters {data.get('clusters')} 内——四维严格嵌套"
                )
        env_tier = _normalize_runbook_env(env)
        if env_tier:
            for cluster in data.get("clusters") or []:
                cenv = _cluster_env(home, str(cluster))
                if cenv and _normalize_runbook_env(cenv) != env_tier:
                    raise ValueError(
                        f"runbook {name} 的 env={env_tier} 与集群 {cluster!r} 的环境"
                        f"{cenv!r} 不一致（env ⊇ cluster）"
                    )
            for host in data.get("hosts") or []:
                henv = _host_env(home, str(host))
                if henv and _normalize_runbook_env(henv) != env_tier:
                    raise ValueError(
                        f"runbook {name} 的 env={env_tier} 与主机 {host!r} 的环境"
                        f"{henv!r} 不一致"
                    )
        for s in steps:
            if not isinstance(s, dict):
                continue
            action = s.get("action")
            params = s.get("params") or {}
            if action == "runbook":
                # YAPL 主框架阶段 C（OPS-DELTA #88）：runbook 动作的 params 是
                # 嵌套引用（{ref, type}）+ 编译工具参数（type=tool 时任意契约
                # params，如 target 是普通字符串参数）——不是 runbook 级拓扑
                # target，跳过引用层 target 校验（工具参数按编译契约 schema
                # 校验，见 contract compile）。
                continue
            target = params.get("target")
            if isinstance(target, str) and _VAR_REF_RE.search(target):
                continue  # 执行期注入，静态不校验
            if isinstance(target, str) and target.strip():
                kind_of = idx.get(target.strip())
                if kind_of is None:
                    raise ValueError(
                        f"runbook {name} 步骤 {s.get('id')!r} 的 target {target!r} 不在"
                        "拓扑表——target 是拓扑实体引用（service/host/host_group/cluster），"
                        "先 topo_query 确认实体名"
                    )
                restricted = _TARGET_TYPE_RESTRICTIONS.get(action)
                if restricted is not None and kind_of not in restricted:
                    raise ValueError(
                        f"runbook {name} 步骤 {s.get('id')!r} 的 {action}.target "
                        f"{target!r} 类型不兼容——{action} 只接受 "
                        f"{sorted(restricted)} 类型，收到 {kind_of}"
                    )
            for key in ("source", "dest"):
                ref = params.get(key)
                if isinstance(ref, dict) and isinstance(ref.get("host"), str):
                    h = ref["host"]
                    if idx.get(h) != "host" and h not in host_endpoints:
                        raise ValueError(
                            f"runbook {name} 步骤 {s.get('id')!r} 的 {key}.host {h!r} 不在"
                            "拓扑表主机（name 或 endpoint）——transfer_file 的 host 必须是"
                            "拓扑表主机"
                        )
            if action == "run_script" and isinstance(params.get("script"), str):
                script = params["script"].strip()
                if script.startswith("steps.") or script.startswith("trigger_context."):
                    continue
        # rollback 场景内的步骤同样做 target 引用校验
        for scenario in rollback:
            if not isinstance(scenario, dict):
                continue
            for s in scenario.get("steps") or []:
                if not isinstance(s, dict):
                    continue
                if s.get("action") == "runbook":
                    continue  # 同 runbook 动作语义：params 非拓扑 target
                target = (s.get("params") or {}).get("target")
                if isinstance(target, str) and target.strip() \
                        and not _VAR_REF_RE.search(target):
                    kind_of = idx.get(target.strip())
                    if kind_of is None:
                        raise ValueError(
                            f"runbook {name} rollback 场景 {scenario.get('name')!r} 步骤"
                            f" {s.get('id')!r} 的 target {target!r} 不在拓扑表"
                        )

    # ── 关系层（变量引用 + 交叉引用）──
    _validate_v2_var_refs(data, name)
    _validate_runbook_refs(data, name, home)


def _host_group_owner(home: Optional[Path], host_group: str) -> str:
    try:
        from tools.topo_tools import load_topology
        for c in (load_topology(home) or {}).get("clusters") or []:
            if isinstance(c, dict) and host_group in (c.get("host_groups") or []):
                return str(c.get("name") or "")
    except Exception:
        pass
    return ""


def _host_cluster(home: Optional[Path], host: str) -> str:
    try:
        from tools.topo_tools import load_topology
        for h in (load_topology(home) or {}).get("hosts") or []:
            if isinstance(h, dict) and str(h.get("name")) == host:
                return str(h.get("cluster") or "")
    except Exception:
        pass
    return ""


def _host_env(home: Optional[Path], host: str) -> str:
    try:
        from tools.topo_tools import load_topology
        for h in (load_topology(home) or {}).get("hosts") or []:
            if isinstance(h, dict) and str(h.get("name")) == host:
                return str(h.get("env") or "")
    except Exception:
        pass
    return ""


def _cluster_env(home: Optional[Path], cluster: str) -> str:
    try:
        from tools.topo_tools import load_topology
        for c in (load_topology(home) or {}).get("clusters") or []:
            if isinstance(c, dict) and str(c.get("name")) == cluster:
                return str(c.get("env") or "")
    except Exception:
        pass
    return ""


def _validate_runbook(data: Dict[str, Any], name: str,
                      home: Optional[Path] = None) -> None:
    """Raise ValueError when a runbook violates schema v0.1 or v0.2."""
    if _is_v2_runbook(data):
        _validate_runbook_v2(data, name, home)
        return
    if data.get("name") != name:
        raise ValueError(f"runbook 内 name({data.get('name')!r}) 与文件名({name!r})不一致")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError(f"runbook {name} 缺少 title")
    if not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError(f"runbook {name} 缺少非空 steps")
    env = data.get("env")
    if env is not None and _normalize_runbook_env(env) is None:
        raise ValueError(
            f"runbook {name} env 非法: {env!r}（可选 local/test/dev/prod；"
            "老值 uat/staging 按档位映射）"
        )
    for step in data["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            raise ValueError(f"runbook {name} 的步骤缺少字符串 id")
        has_commands = isinstance(step.get("commands"), list) and bool(step["commands"])
        has_verify = isinstance(step.get("verify"), str)
        if not (has_commands or has_verify):
            raise ValueError(f"runbook {name} 步骤 {step.get('id')!r} 缺少 commands 或 verify")
    if _is_checklist_runbook(data):
        if not any(isinstance(s.get("verify"), str) and isinstance(s.get("expect"), str)
                   for s in data["steps"]):
            raise ValueError(f"checklist runbook {name} 必须含 verify+expect 的真实验证步骤")
        if not isinstance(data.get("rollback"), list) or not data["rollback"]:
            raise ValueError(f"checklist runbook {name} 必须含非空 rollback（回滚预案）")


def _session_env() -> str:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return str(cfg.get("ops", {}).get("permissions", {}).get("env") or "").strip().lower()
    except Exception:
        return ""


def _ops_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return cfg.get("ops", {}) or {}
    except Exception:
        return {}


_warned_ignored_runbooks: set = set()


def _ignored_runbook_files(home: Optional[Path] = None) -> list:
    """runbooks/ 下会被静默忽略的非 ``.yaml`` 文件（§AU 可见性门控）。"""
    runbooks_dir = _runbooks_dir(home or _hermes_home())
    try:
        if not runbooks_dir.is_dir():
            return []
        return sorted(
            p.name for p in runbooks_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() != ".yaml"
            and not p.name.startswith(".")
        )
    except Exception:
        return []


def _warn_ignored_runbook_files(home: Optional[Path] = None) -> None:
    """非 ``.yaml`` runbook 文件的存在必须可见（§AU：静默失败 = 误导排查）。

    发现 runbooks/ 下有不会被加载的文件（.md/.yml/其他）→ logger.warning
    点名文件。同一 runbooks 目录只警告一次（check_fn 每 30s 重跑，去重防刷屏）。
    """
    try:
        runbooks_dir = _runbooks_dir(home or _hermes_home())
    except Exception:
        return
    ignored = _ignored_runbook_files(home)
    if not ignored:
        return
    key = str(runbooks_dir)
    if key in _warned_ignored_runbooks:
        return
    _warned_ignored_runbooks.add(key)
    shown = ", ".join(ignored[:5])
    more = f" 等 {len(ignored)} 个" if len(ignored) > 5 else ""
    logger.warning(
        "runbooks/ 发现 %d 个非 .yaml 文件被忽略（%s%s）——runbook 仅支持 "
        "schema v0.1 的 .yaml，.md/其他格式不会被 runbook_load 加载；"
        "如需普通文档请放到 runbooks/ 之外。",
        len(ignored), shown, more,
    )


def _runbook_data_exists(home: Optional[Path] = None) -> bool:
    """runbooks/ 目录存在且含至少一个 yaml。"""
    runbooks_dir = _runbooks_dir(home or _hermes_home())
    try:
        if not runbooks_dir.is_dir():
            return False
        return any(runbooks_dir.glob("*.yaml"))
    except Exception:
        return False


def check_runbook_requirements() -> bool:
    """Runbook tools are available by default when runbook data exists.

    OPS-DELTA #1：能力门控按数据存在性自动切换（装上即用，不再要求 ops-init
    先行）。config 的 ``ops.runbooks.enabled`` 保留为覆盖开关：
      - 缺省（无该键）→ 按数据存在性（runbooks/ 有 yaml 即可用）；
      - 显式 ``enabled: true`` → 仍要求数据就位；
      - 显式 ``enabled: false`` → 始终关闭（向后兼容既有关闭配置）。

    OPS-DELTA 批次四十 §AU：门控检查同时扫描 runbooks/ 下的非 ``.yaml``
    文件并发警告（格式门控可见，不再静默忽略）。
    """
    ops = _ops_config()
    if ops.get("runbooks", {}).get("enabled") is False:
        return False
    _warn_ignored_runbook_files()
    return _runbook_data_exists()


# ---------------------------------------------------------------------------
# Checklist state (L4 部署阶段门)
# ---------------------------------------------------------------------------

def _read_checklist_state(home: Path) -> Dict[str, Any]:
    path = _state_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("runbook: failed to parse state %s: %s", path, exc)
        return {}


def _write_checklist_state(home: Path, state: Dict[str, Any]) -> None:
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _checklist_state_for(home: Path, name: str) -> Dict[str, Any]:
    return (_read_checklist_state(home).get(name) or {})


# ---------------------------------------------------------------------------
# runbook_load
# ---------------------------------------------------------------------------

def _score_runbook(rb: Dict[str, Any], query: str) -> float:
    q = query.strip().lower()
    if not q:
        return 0.0
    haystacks = [rb.get("name", ""), rb.get("title", ""), rb.get("summary", "")]
    haystacks += list(rb.get("triggers") or [])
    hay_lower = [str(h).lower() for h in haystacks if h]
    if any(h == q for h in hay_lower):
        return 100.0
    if any(q in h for h in hay_lower):
        return 50.0 + max(len(h) for h in hay_lower if q in h) * 0.1
    tokens = [t for t in re.split(r"[^\w\u4e00-\u9fff]+", q) if len(t) >= 2]
    if tokens:
        hits = sum(1 for tok in tokens if any(tok in h for h in hay_lower))
        return 10.0 * hits / len(tokens)
    return 0.0


def _summary_line(rb: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": rb.get("name"),
        "title": rb.get("title"),
        "kind": rb.get("kind"),
        "env": rb.get("env"),
        "checklist": _is_checklist_runbook(rb),
    }


def _describe_vault_refs_in_commands(holder: Dict[str, Any], key: str) -> int:
    """把 holder[key] 命令列表里的 <vault:...> 占位符描述化，返回替换次数。"""
    count = 0
    cmds = holder.get(key)
    if not isinstance(cmds, list):
        return 0
    out: List[Any] = []
    for cmd in cmds:
        if isinstance(cmd, str):
            cmd, n = _VAULT_REF_RE.subn(
                lambda m: (
                    f"«vault:{m.group(1)} — 凭据引用（执行时从保险箱/vault 读取，"
                    "明文不进会话记录）»"
                ),
                cmd,
            )
            count += n
        out.append(cmd)
    holder[key] = out
    return count


def _describe_vault_refs(payload: Dict[str, Any]) -> int:
    """遍历 runbook 的 steps/rollback 命令，描述化 <vault:...> 占位符。"""
    count = 0
    for step in payload.get("steps") or []:
        if isinstance(step, dict):
            count += _describe_vault_refs_in_commands(step, "commands")
    for rb in payload.get("rollback") or []:
        if isinstance(rb, dict):
            count += _describe_vault_refs_in_commands(rb, "commands")
    return count


def _full_payload(home: Path, rb: Dict[str, Any]) -> Dict[str, Any]:
    name = rb["name"]
    payload = dict(rb)
    payload["session_env"] = _session_env()
    rb_env = _normalize_runbook_env(rb.get("env"))
    session_env = _normalize_runbook_env(payload["session_env"])
    if rb.get("env") and payload["session_env"] and rb_env and session_env and rb_env != session_env:
        payload["env_mismatch"] = True
        payload["env_warning"] = (
            f"runbook 适用环境 {rb['env']} 与当前会话环境 {payload['session_env']} 不一致；"
            "跨环境操作由命令级权限矩阵逐条判定（L2 及以上走审批），不是整体拒绝。"
        )
    payload["checklist_state"] = _checklist_state_for(home, name) if _is_checklist_runbook(rb) else None
    if _is_v2_runbook(rb):
        payload["note"] = (
            "v0.2 runbook（声明式动作，无命令）：执行器在 P4 实现，当前仅可创建/"
            "校验/预览；步骤动作由执行器按 action × target 类型 × managed_by 生成命令。"
        )
    else:
        payload["note"] = (
            "本工具不执行任何命令；步骤命令由 agent 通过终端执行，逐条过权限矩阵。"
        )
    vault_refs = _describe_vault_refs(payload)
    if vault_refs:
        payload["vault_refs"] = vault_refs
        payload["note"] += (
            f" 本 runbook 含 {vault_refs} 处 <vault:...> 凭据引用：明文只在执行时"
            "由你从保险箱/vault 读取并立即注入命令（命令串过 redact），不要在"
            "对话或命令文本里内插明文。"
        )
    payload["note"] += (
        " 若本次执行有改进（新坑/新命令），可向用户提议更新本 runbook"
        "（runbook_create overwrite=true）。"
    )
    return payload


def runbook_load(
    runbook: Optional[str] = None,
    query: Optional[str] = None,
    home: Optional[Path] = None,
) -> str:
    """Load a runbook by name, fuzzy-match by trigger keywords, or list all."""
    home = home or _hermes_home()
    runbooks_dir = _runbooks_dir(home)
    if not runbooks_dir.is_dir():
        return tool_error(
            f"runbooks 数据不存在（{runbooks_dir}），"
            "先跑 vigil topo-discover 或手工创建 runbooks/*.yaml。"
        )

    if runbook:
        try:
            data = _load_runbook(home, runbook)
        except ValueError as exc:
            return tool_error(str(exc))
        if data is None:
            return tool_error(f"runbook 不存在: {runbook}（可省略参数列出全部）")
        try:
            _validate_runbook(data, runbook, home)
        except ValueError as exc:
            return tool_error(f"runbook 校验失败: {exc}")
        return json.dumps(_full_payload(home, data), ensure_ascii=False, indent=2)

    if query and query.strip():
        scored = []
        for path in sorted(runbooks_dir.glob("*.yaml")):
            if path.name.startswith("."):
                continue
            try:
                data = _load_runbook(home, path.stem)
            except ValueError:
                continue
            if data is None:
                continue
            score = _score_runbook(data, query)
            if score > 0:
                scored.append((score, data))
        scored.sort(key=lambda t: t[0], reverse=True)
        if scored:
            best = scored[0][1]
            payload = _full_payload(home, best)
            payload["alternatives"] = [_summary_line(rb) for _, rb in scored[1:5]]
            return json.dumps(payload, ensure_ascii=False, indent=2)
        return tool_error(
            f"没有 runbook 匹配「{query}」。可用 runbook: "
            + ", ".join(sorted(p.stem for p in runbooks_dir.glob("*.yaml")
                               if not p.name.startswith(".")))
        )

    summaries = []
    for path in sorted(runbooks_dir.glob("*.yaml")):
        if path.name.startswith("."):
            continue
        try:
            data = _load_runbook(home, path.stem)
        except ValueError:
            continue
        if data is not None:
            summaries.append(_summary_line(data))
    return json.dumps({"count": len(summaries), "runbooks": summaries},
                      ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# runbook_checkpoint（L4 部署阶段门）
# ---------------------------------------------------------------------------

def runbook_checkpoint(
    runbook: str,
    step_id: str,
    status: str,
    evidence: str = "",
    reset: bool = False,
    home: Optional[Path] = None,
) -> str:
    """Record an L4 checklist step result; blocks skipping earlier gates."""
    home = home or _hermes_home()
    try:
        data = _load_runbook(home, runbook)
    except ValueError as exc:
        return tool_error(str(exc))
    if data is None:
        return tool_error(f"runbook 不存在: {runbook}")
    try:
        is_v2 = _is_v2_runbook(data)
    except ValueError as exc:
        return tool_error(str(exc))
    if is_v2:
        return tool_error(
            f"runbook {runbook} 是 schema v0.2（声明式动作，无 commands）："
            "checkpoint 是 v0.1 checklist 阶段门；v0.2 请用 runbook_execute 执行"
            "（执行器生成命令 + 矩阵审批门 + expect/on_failure + 执行记录）。"
        )
    if not _is_checklist_runbook(data):
        return tool_error(f"runbook {runbook} 不是 checklist runbook（kind=deploy, checklist=true），无需 checkpoint")
    if status not in _VALID_STATUSES:
        return tool_error(f"status 必须是 pass/fail，收到 {status!r}")

    steps = data.get("steps") or []
    ids = [s.get("id") for s in steps]
    if step_id not in ids:
        return tool_error(f"runbook {runbook} 没有步骤 {step_id!r}（可用: {', '.join(ids)}）")

    state = _read_checklist_state(home)
    rb_state = _checklist_state_for(home, runbook)
    if reset:
        rb_state = {}

    idx = ids.index(step_id)
    blocked = [sid for sid in ids[:idx] if rb_state.get(sid, {}).get("status") != "pass"]
    if blocked:
        return tool_error(
            f"阶段门拒绝推进：步骤 {step_id!r} 的前置步骤未全部通过 "
            f"({', '.join(blocked)})。先完成前置核对（runbook_load 查看步骤），"
            "通过后 runbook_checkpoint(..., status='pass') 再推进。"
        )

    rb_state[step_id] = {
        "status": status,
        "evidence": evidence or "",
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    state[runbook] = rb_state
    _write_checklist_state(home, state)
    return json.dumps(
        {"runbook": runbook, "recorded": step_id, "status": status,
         "checklist_state": rb_state, "note": "阶段门状态已记录（审计用）。"},
        ensure_ascii=False, indent=2,
    )


# ---------------------------------------------------------------------------
# runbook_create（正规创建入口）
# ---------------------------------------------------------------------------

# ── 批次四十四 §BN/§BL：语义识别替代词面匹配 ────────────────────────────────
# 命令白名单：这些命令的 ``-p`` 明确是"密码"语义（sshpass -p SECRET、
# mysql/mysqldump -p SECRET、pg_dump/psql 之外的工具等）。其余命令的 ``-p``
# 是端口/无值 flag/保时间戳（docker/scp/psql 端口、mkdir 无值 flag），绝不
# 当密码。原则：语义 > 词面，宁可漏给人工审，不可误杀逼绕词。
_PASSWORD_FLAG_COMMANDS = frozenset({
    "sshpass",
    "mysql",
    "mysqldump",
    "mysqlimport",
    "mariadb",
    "mariadb-dump",
    "pg_dump",
    "pg_restore",
    "redis-cli",
    "mongosh",
    "mongodump",
    "sqlplus",
    "sqlcmd",
    "psql",
})


def _first_command_name(cmd: str) -> str:
    """取命令第一个可执行名（basename），以便按命令类型判定 ``-p`` 语义。"""
    if not cmd:
        return ""
    stripped = cmd.lstrip()
    # 跳过常见前缀（sudo/env/CHAIN 环境变量赋值）拿真实命令。
    toks = stripped.split()
    name = ""
    for tok in toks:
        if tok in ("sudo", "env", "nohup", "time"):
            continue
        if "=" in tok and not tok.startswith("-"):
            continue  # 前导环境变量赋值 VAR=...
        name = tok
        break
    return name.split("/")[-1].strip("'\"")


def _looks_like_attribute(value: str) -> bool:
    """值形态是否像普通属性（端口/路径/变量/引用/URL）而非明文密码。

    True → 放行（不是密码候选）；False → 是密码候选。
    """
    v = (value or "").strip().strip('\'"')
    if not v:
        return True  # 无值 flag → 非密码
    if v.startswith("<vault:"):
        return True
    # 端口/数字：docker -p 8080:80、psql -p 5432
    if v[0].isdigit():
        return True
    # 路径或 URL：mkdir -p /a/b、-i /root/x.pem、VAULT_PASS=secret/data/...
    if "/" in v or "\\" in v or v.startswith(".") or ":" in v or "://" in v:
        return True
    # 变量：$VAR、${VAR}、%(name)s
    if v.startswith("$") or "${" in v or "%(" in v:
        return True
    # 引用形态包含路径/占位符特征。
    return False


def _is_password_flag(flag: str, value: str, cmd: str) -> bool:
    """``-p`` 是否为密码：命令在密码白名单 AND 值形态是密码候选。

    ``-p``（小写）在密码白名单命令里通常是密码；``-P``（大写）在各数据库
    工具里是端口（mysql/redis-cli -P 端口、pg_dump -P 密码属少数，此处让位于
    端口），一律放行，避免误杀端口形态。
    """
    if flag == "-P":
        return False  # -P 在 DB 工具里是端口，不按密码处理
    if flag != "-p":
        return True  # --password/--token/--api-key 词面明确，保持严格
    cmd_name = _first_command_name(cmd)
    if cmd_name not in _PASSWORD_FLAG_COMMANDS:
        return False
    # 密码命令里 `-p` 后无值（等下一行/无值 flag）→ 非密码。
    value = (value or "").strip().strip("'\"")

    if not value:
        return False
    # 值形态是属性（端口/路径/变量/引用）→ 放行。
    if _looks_like_attribute(value):
        return False
    return True


def _assign_value_is_plaintext(value: str) -> bool:
    """赋值式（PASSWORD=... / --password=...）是否真明文。

    值形态是引用/路径/URL/vault 占位符/变量 → 放行（VAULT_PASS=secret/data/...
    是指向 secret 的引用，不是明文）；纯短串（无 /、$、<vault:、: 等特征）才
    是真明文。
    """
    v = (value or "").strip().strip('\'"')
    if not v:
        return False
    if v.startswith("<vault:"):
        return False
    if _looks_like_attribute(v):
        return False
    return True


def _find_plaintext_secret(cmd: str) -> Optional[str]:
    """返回命令中疑似明文凭据的键名（password/--password/-p 等），无则 None。

    <vault:path/field> 占位符放行（既有机制）。批次四十四 §BN/§BL 语义化：
      - ``-p`` 只在密码命令白名单（sshpass/mysql/mysqldump/pg_dump/redis-cli/
        mongosh 等）且值是密码候选（非数字/路径/变量/引用）时才算密码；
        docker/scp/psql 端口、mkdir 无值 flag、-i 私钥路径一律放行。
      - 赋值式命中（PASSWORD=hunter2）只在值是真明文时拦截；值是指向 secret
        的路径/引用（VAULT_PASS=secret/data/... / $VAR / <vault:>）放行。
    --password/--token/--api-key 词面语义明确是凭据，保持严格。
    只报键名不报值——凭据值绝不出现在错误消息/日志里。
    """
    if not cmd or not isinstance(cmd, str):
        return None
    for m in _PLAINTEXT_SECRET_FLAG_RE.finditer(cmd):
        # 三组交替：long-flag(空格/=) | -p(空格/=或紧贴)。归一取 flag+value。
        if m.group(1) is not None and m.group(2) is not None:
            flag, value = m.group(1), m.group(2)
        elif m.group(3) is not None and m.group(4) is not None:
            flag, value = "-" + m.group(3), m.group(4)
        else:
            continue
        if not _is_password_flag(flag, value, cmd):
            continue
        if value.startswith("<vault:"):
            continue
        return flag
    m = _PLAINTEXT_SECRET_ASSIGN_RE.search(cmd)
    if m:
        if m.group(2).startswith("<vault:"):
            return None
        if _assign_value_is_plaintext(m.group(2)):
            return m.group(1)
    m = _PLAINTEXT_CRED_FLAG_RE.search(cmd)
    if m and "<vault:" not in m.group(2):
        return m.group(1)
    return None


def _secret_error_hint(key: str) -> str:
    """命中明文凭据时的纠错引导（批次四十四 §BL 建议）——学着改对，不自己绕词。"""
    return (
        f"疑似含明文凭据（{key}）。正确写法：优先用受控凭据通道——"
        "目标主机经 topo_query 取凭据后经 vssh/sudo_exec 注入，或命令里用 "
        "<vault:path/field> 占位符（执行时由你从保险箱/vault 读取并立即注入）。"
        "若这只是本地文件路径/端口/连接参数而非凭据，可直接写（本校验只拦真明文）。"
    )


def _scan_commands_for_secrets(steps: List[Dict[str, Any]], rollback: Optional[List[Any]]) -> Optional[str]:
    """扫描 steps/rollback 的 commands，命中明文凭据返回可读错误，无则 None。

    错误只报键名不报值；附带正确写法示例（§BL），让被拦的 agent 知道怎么改对
    而非摸索绕词。
    """
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        for cmd in step.get("commands") or []:
            key = _find_plaintext_secret(cmd)
            if key:
                return f"步骤 {step.get('id')!r} 的命令{_secret_error_hint(key)}"
    for rb in rollback or []:
        if not isinstance(rb, dict):
            continue
        for cmd in rb.get("commands") or []:
            key = _find_plaintext_secret(cmd)
            if key:
                return f"rollback 命令{_secret_error_hint(key)}"
    return None


def _runbook_v2_actions(data: Dict[str, Any]) -> List[str]:
    """收集 runbook（含 rollback 场景）用到的全部动作（资产审批矩阵判定用）。"""
    acts: List[str] = []
    for step in data.get("steps") or []:
        if isinstance(step, dict) and step.get("action"):
            acts.append(str(step["action"]))
    for scenario in data.get("rollback") or []:
        if not isinstance(scenario, dict):
            continue
        for step in scenario.get("steps") or []:
            if isinstance(step, dict) and step.get("action"):
                acts.append(str(step["action"]))
    return sorted(set(acts))


def _asset_approve_runbook(data: Dict[str, Any], name: str, home: Path,
                           env: str) -> tuple:
    """v0.2 runbook 资产审批（YAPL P3 §11.4 双审批层次之资产审批）。

    Returns (approved: bool, markers: Dict[str, str], error: str)。

    语义（§11.5）：
    - 强制人工：runbook 任一动作在矩阵对应 env 为 ``{approve: required}`` →
      force_manual（不 smart、覆盖 approvals.mode、不提供 allowlist）；
    - smart 自动批准：approvals.mode=smart 且全部动作矩阵判 execute 级
      （矩阵是权限唯一裁决——execute 级动作执行时本就免审批，创建时同样
      不需要人工在场；确定性智能，不引入 aux LLM）；
    - approvals.mode=off 且无强制人工 → 跳过（与全系统 mode=off 语义一致）；
    - 其余 → 人工门（fail-closed：无人在场 BLOCK，永不无人落盘）。
    矩阵缺失 → 全部动作默认 approve（保守），不触发强制人工。
    """
    from tools.approval import request_asset_approval
    from tools.matrix_data import get_level, load_matrix_or_empty

    acts = _runbook_v2_actions(data)
    matrix = load_matrix_or_empty(home)
    levels = {a: get_level(matrix, env, a) for a in acts}
    force_manual = any(v["level"] == "required" for v in levels.values())
    smart_low_risk = bool(acts) and all(v["level"] == "execute" for v in levels.values())
    action_desc = ", ".join(acts) or "(无动作)"
    result = request_asset_approval(
        asset_type="runbook",
        asset_name=name,
        description=(
            f"runbook {name}（env={env or 'local'}）内容审批——"
            f"动作: {action_desc}（矩阵裁决：{'含强制人工高危动作' if force_manual else '常规档位'}）"
        ),
        env=env or "local",
        force_manual=force_manual,
        smart_low_risk=smart_low_risk,
    )
    if not result.get("approved"):
        return False, {}, str(result.get("message") or "资产审批未通过")
    markers = {
        "approved_by": str(result.get("approved_by") or "manual"),
    }
    return True, markers, ""


def runbook_create(
    runbook: str,
    title: str,
    triggers: Optional[List[Any]] = None,
    summary: str = "",
    steps: Optional[List[Any]] = None,
    rollback: Optional[List[Any]] = None,
    env: Optional[str] = None,
    kind: str = "incident",
    overwrite: bool = False,
    clusters: Optional[List[str]] = None,
    host_groups: Optional[List[str]] = None,
    hosts: Optional[List[str]] = None,
    schedule: Optional[Dict[str, Any]] = None,
    on_failure: Optional[Any] = None,
    home: Optional[Path] = None,
) -> str:
    """Create/overwrite a structured runbook (runbooks/<name>.yaml, v0.1/v0.2).

    双 schema：steps 用 ``commands`` → v0.1（存量风格）；steps 用 ``action``
    → v0.2（声明式动作，命令彻底消失，yapl-design.md §10）。v0.2 写 version: 2，
    走三层校验（结构/引用/关系）**+ 资产审批**（YAPL P3 §11.4：创建/变更过人工
    审批门；矩阵判含 {approve: required} 高危动作 → 强制人工不 smart；审批通过
    后落盘带 approved_at/approved_by/approved_version 预审标记，P4 调度器执行
    豁免用）。v0.1 保留原路径（不经资产审批，OPS-DELTA #69 注明过渡期）。
    Fail-closed：严格 kebab-case 名称（防路径穿越）、非空 steps、v0.1 commands
    拒绝疑似明文凭据（用 <vault:path/field> 占位符）、同名已存在需 overwrite=True。
    写盘前复用 ``_validate_runbook`` 校验，保证 runbook_load 能原样加载回来。
    env 接受四值 local/test/dev/prod；老值 uat→prod、staging→dev 按档位映射后落盘
    （create 是新写入，直接规范到新枚举；load 保留文件原值）。
    """
    home = home or _hermes_home()
    name = str(runbook or "").strip()
    if not _CREATE_NAME_RE.match(name):
        return tool_error(
            f"runbook 名称非法: {name!r}——必须 kebab-case（小写字母/数字，"
            "连字符分隔，如 ansible-syntax-check）。"
        )
    if not isinstance(title, str) or not title.strip():
        return tool_error("title 必填且不能为空。")
    mapped_env = None
    if env is not None:
        mapped_env = _normalize_runbook_env(env)
        if mapped_env is None:
            return tool_error(
                f"env 非法: {env!r}（可选 local/test/dev/prod；老值 uat/staging "
                "按档位映射）。"
            )
    if not isinstance(steps, list) or not steps:
        return tool_error("steps 必填且不能为空（[{id, title, commands: [...]}]）。")
    if not all(isinstance(s, dict) for s in steps):
        return tool_error("steps 每项必须是对象。")
    v2_style = any("action" in s for s in steps)
    v1_style = any("commands" in s for s in steps)
    if v2_style and v1_style:
        return tool_error(
            "steps 不得混用 v0.1 commands 与 v0.2 action——一次 runbook 只用一种风格："
            "v0.1 存量用 commands；v0.2 用 action+params（命令彻底消失）。"
        )
    if not v2_style and not v1_style:
        return tool_error("steps 每项必须含 commands（v0.1）或 action（v0.2）之一。")

    data: Dict[str, Any] = {}
    if v2_style:
        if kind not in _V2_KINDS:
            return tool_error(f"kind 必须是 {sorted(_V2_KINDS)} 之一（v0.2 默认 incident）。")
        data = {
            "name": name,
            "title": title.strip(),
            "version": 2,
            "kind": kind,
        }
        if mapped_env:
            data["env"] = mapped_env
        if triggers:
            data["triggers"] = triggers
        if isinstance(summary, str) and summary.strip():
            data["summary"] = summary.strip()
        if clusters:
            data["clusters"] = list(clusters)
        if host_groups:
            data["host_groups"] = list(host_groups)
        if hosts:
            data["hosts"] = list(hosts)
        if schedule:
            data["schedule"] = schedule
        if on_failure is not None:
            data["on_failure"] = on_failure
        data["steps"] = steps
        if rollback:
            data["rollback"] = rollback
    else:
        if kind not in _VALID_KINDS:
            return tool_error(f"kind 必须是 {sorted(_VALID_KINDS)} 之一（默认 incident）。")
        for step in steps:
            if not isinstance(step.get("id"), str) or not step["id"].strip():
                return tool_error("steps 每项必须含非空字符串 id。")
            cmds = step.get("commands")
            if not isinstance(cmds, list) or not cmds or not all(isinstance(c, str) for c in cmds):
                return tool_error(f"步骤 {step.get('id')!r} 的 commands 必须是非空字符串列表。")
        if rollback is not None:
            if not isinstance(rollback, list):
                return tool_error("rollback 必须是列表（[{title, commands}]）。")
            for rb in rollback:
                if not isinstance(rb, dict):
                    return tool_error("rollback 每项必须是对象 {title, commands}。")
                cmds = rb.get("commands")
                if not isinstance(cmds, list) or not cmds or not all(isinstance(c, str) for c in cmds):
                    return tool_error("rollback 每项必须含非空 commands 字符串列表。")
        secret_err = _scan_commands_for_secrets(steps, rollback)
        if secret_err:
            return tool_error(secret_err)
        data = {
            "name": name,
            "title": title.strip(),
            "version": 1,
            "kind": kind,
        }
        if mapped_env:
            data["env"] = mapped_env
        if triggers:
            cleaned = [str(t).strip() for t in triggers if isinstance(t, str) and t.strip()]
            if cleaned:
                data["triggers"] = cleaned
        if isinstance(summary, str) and summary.strip():
            data["summary"] = summary.strip()
        data["steps"] = steps
        if rollback:
            data["rollback"] = rollback

    runbooks_dir = _runbooks_dir(home)
    path = runbooks_dir / f"{name}.yaml"
    exists = path.is_file()

    # batch74（OPS-DELTA #89）：新 runbook 强制 v0.2 声明式格式——v0.1 commands
    # 裸命令绕过 resolve_topo_ref / 动作词表 / 矩阵裁决（§六 LLM 绕行入口教训
    # 第三例）。v0.1 仅允许 overwrite 磁盘上已存在的 v0.1 存量文件；磁盘已是
    # v0.2 时覆盖提交必须保持 v0.2（不能把 v0.2 降级成 v0.1）。
    if not v2_style:
        if not exists:
            actions = _v2_actions(home) or []
            actions_text = "/".join(actions) if actions else "（schemas.yaml actions 词表）"
            return tool_error(
                "新 runbook 必须使用 v0.2 声明式格式（steps 用 action+params，命令彻底"
                f"消失；action ∈ 动作词表 {actions_text}，params 按动作契约填必填）。"
                "v0.1 commands 格式仅供 overwrite 存量文件。"
                "参考：{action: restart, params: {target: <拓扑实体>}} 或 "
                "{action: scale, params: {target: <实体>, replicas: N}}"
            )
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if _is_v2_runbook(existing):
                return tool_error(
                    f"runbook 已是 v0.2 格式: {name}——覆盖提交必须保持 v0.2 声明式格式"
                    "（steps 用 action+params），不能降级为 v0.1 commands 格式。"
                )
        except Exception:
            return tool_error(
                f"无法读取存量 runbook: {name}（{path}）——v0.1 commands 格式仅允许"
                "覆盖可读的 v0.1 存量文件；请改用 v0.2 格式或先修复存量文件。"
            )

    # 复用既有校验器：保证 runbook_load 能原样加载回来（checklist 规则一并生效）。
    try:
        _validate_runbook(data, name, home)
    except ValueError as exc:
        return tool_error(f"runbook 校验失败: {exc}")

    if exists and not overwrite:
        return tool_error(
            f"runbook 已存在: {name}（{path}）。需要覆盖请 overwrite=true。"
        )

    # YAPL P3 资产审批（§11.4）：v0.2 创建/变更过审批门（复用 approvals 机制，
    # 矩阵 {approve: required} 高危动作强制人工）；v0.1 保留原路径（OPS-DELTA
    # #69 注明过渡期）。审批失败不落盘 + 报错引导。
    if v2_style:
        approved, markers, approve_err = _asset_approve_runbook(
            data, name, home, mapped_env or "local"
        )
        if not approved:
            return tool_error(
                f"runbook 资产审批未通过，未落盘: {approve_err}（内容未写入 "
                f"{path}）。请修改后重试，或由用户在交互会话中重新创建。"
            )
        # 预审标记（执行豁免数据模型，P4 调度器接线）：approved_at / approved_by /
        # approved_version（内容哈希——文件被改动后哈希漂移 = 豁免失效）。
        approved_version = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False,
                       default=str).encode("utf-8")
        ).hexdigest()[:16]
        data["approved_at"] = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        data["approved_by"] = markers.get("approved_by") or "manual"
        data["approved_version"] = approved_version

    try:
        runbooks_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception as exc:
        return tool_error(f"写入 runbook 失败: {exc}")

    # YAPL P4（§10.7）：带 schedule 的 runbook 落盘后同步注册到现有 cron 调度器
    # （幂等：按 runbook 标记更新/注销）。失败只记日志，不阻断落盘（调度可
    # 后续用 vigil cron 手动补）。
    try:
        from tools.runbook_schedule import register_runbook_schedule
        schedule = data.get("schedule") if v2_style else None
        register_runbook_schedule(name, schedule, home)
    except Exception as exc:
        logger.warning("runbook 调度注册失败（不影响落盘）: %s", exc)

    return json.dumps(
        {
            "status": "updated" if exists else "created",
            "name": name,
            "path": str(path),
            "steps": len(steps),
            "note": (
                ("已创建/更新 runbook（schema v0.2，声明式动作，已过资产审批）。"
                 "预审标记 approved_at/approved_by/approved_version 已落盘（P4 执行豁免）。"
                 "执行器在 P4 实现；runbook_load 可加载。")
                if v2_style else
                ("已创建/更新 runbook（schema v0.1）。runbook_load 可加载；"
                 "若意图是行为约束，触发词已写入 triggers。")
            ),
        },
        ensure_ascii=False, indent=2,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _load_handler(args: Dict[str, Any], **kwargs) -> str:
    return runbook_load(
        runbook=args.get("runbook"),
        query=args.get("query"),
    )


def _checkpoint_handler(args: Dict[str, Any], **kwargs) -> str:
    return runbook_checkpoint(
        runbook=args.get("runbook", ""),
        step_id=args.get("step_id", ""),
        status=args.get("status", ""),
        evidence=args.get("evidence", ""),
        reset=bool(args.get("reset", False)),
    )


def _create_handler(args: Dict[str, Any], **kwargs) -> str:
    return runbook_create(
        runbook=args.get("runbook", ""),
        title=args.get("title", ""),
        triggers=args.get("triggers") or [],
        summary=args.get("summary") or "",
        steps=args.get("steps") or [],
        rollback=args.get("rollback"),
        env=args.get("env"),
        kind=args.get("kind") or "incident",
        overwrite=bool(args.get("overwrite", False)),
        clusters=args.get("clusters"),
        host_groups=args.get("host_groups"),
        hosts=args.get("hosts"),
        schedule=args.get("schedule"),
        on_failure=args.get("on_failure"),
    )


registry.register(
    name="runbook_load",
    toolset="runbook",
    schema=_DEFAULT_LOAD_SCHEMA,
    handler=_load_handler,
    check_fn=check_runbook_requirements,
    emoji="📘",
    max_result_size_chars=30_000,
)

registry.register(
    name="runbook_checkpoint",
    toolset="runbook",
    schema=_DEFAULT_CHECKPOINT_SCHEMA,
    handler=_checkpoint_handler,
    check_fn=check_runbook_requirements,
    emoji="✅",
    max_result_size_chars=30_000,
)

registry.register(
    name="runbook_create",
    toolset="runbook",
    schema=_DEFAULT_CREATE_SCHEMA,
    handler=_create_handler,
    # batch76（OPS-DELTA #91）：runbook_create 必须始终可用——runbooks/ 为空
    # 正是它该工作的时候（引导死锁：目录空 → 工具消失 → LLM 绕行直接写 YAML
    # 文件绕过资产审批/三层校验器）。存在性门控只保留在 runbook_load /
    # runbook_checkpoint（读/执行依赖存量数据）；create 内部同名保护（exists
    # and not overwrite）+ _validate_runbook + 资产审批不受影响。
    emoji="📝",
    max_result_size_chars=30_000,
)
