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
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

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
_PLAINTEXT_SECRET_FLAG_RE = re.compile(
    r"(?i)(--password|--passwd|--token|--api-key|-p)\s+(\S+)"
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
        "创建/更新运维 runbook（程序层：结构化 YAML，runbooks/<name>.yaml，schema v0.1）。"
        "runbook 只支持 .yaml——.md/其他格式不会被加载。"
        "runbook 是 Vigil 程序层机制（触发条件 + 步骤 + 命令 + 回滚），不是 Markdown 文档："
        "runbook_load 可按名/触发词加载，runbook_checkpoint 可门控部署阶段。"
        "用户说'沉淀/记录/保存为 runbook'时应调用本工具。命令一律拒绝明文密码/token——"
        "用 <vault:path/field> 占位符（执行时从保险箱读取注入）。同名已存在需 "
        "overwrite=true 才覆盖。"
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
                    "步骤列表，每步 {id, title, commands: [命令]}；checklist 步骤"
                    "可带 verify+expect（真实验证）。"
                ),
            },
            "rollback": {
                "type": "array",
                "description": "回滚步骤（可选），[{title, commands}]。",
            },
            "env": {
                "type": "string",
                "description": "适用环境 local/test/dev/prod（可选，默认不限制；老值 uat/staging 按档位映射）。",
            },
            "kind": {
                "type": "string",
                "enum": ["deploy", "incident", "checklist"],
                "description": "deploy/incident/checklist（默认 incident）。",
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


def _validate_runbook(data: Dict[str, Any], name: str) -> None:
    """Raise ValueError when a runbook violates schema v0.1."""
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
            _validate_runbook(data, runbook)
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

def _find_plaintext_secret(cmd: str) -> Optional[str]:
    """返回命令中疑似明文凭据的键名（password/--password/-p 等），无则 None。

    <vault:path/field> 占位符放行（既有机制）；``-p`` 后接数字开头的值（如
    docker/ssh 端口发布 ``-p 8080:80``）不算凭据。只报键名不报值——凭据值
    绝不出现在错误消息/日志里。
    """
    if not cmd or not isinstance(cmd, str):
        return None
    m = _PLAINTEXT_SECRET_ASSIGN_RE.search(cmd)
    if m:
        if m.group(2).startswith("<vault:"):
            return None
        return m.group(1)
    for m in _PLAINTEXT_SECRET_FLAG_RE.finditer(cmd):
        flag, value = m.group(1), m.group(2)
        if flag == "-p" and value and value[0].isdigit():
            continue
        if value.startswith("<vault:"):
            continue
        return flag
    m = _PLAINTEXT_CRED_FLAG_RE.search(cmd)
    if m and "<vault:" not in m.group(2):
        return m.group(1)
    return None


def _scan_commands_for_secrets(steps: List[Dict[str, Any]], rollback: Optional[List[Any]]) -> Optional[str]:
    """扫描 steps/rollback 的 commands，命中明文凭据返回可读错误，无则 None。"""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        for cmd in step.get("commands") or []:
            key = _find_plaintext_secret(cmd)
            if key:
                return (
                    f"步骤 {step.get('id')!r} 的命令疑似含明文凭据（{key}），"
                    "runbook 拒绝明文密码/token——请改用 <vault:path/field> 占位符"
                    "（执行时由你从保险箱/vault 读取并立即注入）。"
                )
    for rb in rollback or []:
        if not isinstance(rb, dict):
            continue
        for cmd in rb.get("commands") or []:
            key = _find_plaintext_secret(cmd)
            if key:
                return (
                    f"rollback 命令疑似含明文凭据（{key}），请改用 <vault:path/field>"
                    "占位符（执行时从保险箱读取注入）。"
                )
    return None


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
    home: Optional[Path] = None,
) -> str:
    """Create/overwrite a structured runbook (runbooks/<name>.yaml, schema v0.1).

    Fail-closed: 严格 kebab-case 名称（防路径穿越）、非空 steps/commands、
    commands 拒绝疑似明文凭据（用 <vault:path/field> 占位符）、同名已存在需
    overwrite=True。写盘前复用 ``_validate_runbook`` 校验，保证 runbook_load
    能原样加载回来（checklist/deploy 规则一并生效）。env 接受四值
    local/test/dev/prod；老值 uat→prod、staging→dev 按档位映射后落盘（create
    是新写入，直接规范到新枚举；load 保留文件原值）。
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
    if kind not in _VALID_KINDS:
        return tool_error(f"kind 必须是 {sorted(_VALID_KINDS)} 之一（默认 incident）。")
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
    for step in steps:
        if not isinstance(step, dict):
            return tool_error("steps 每项必须是对象 {id, title, commands}。")
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

    runbooks_dir = _runbooks_dir(home)
    path = runbooks_dir / f"{name}.yaml"
    exists = path.is_file()

    data: Dict[str, Any] = {
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

    # 复用既有校验器：保证 runbook_load 能原样加载回来（checklist 规则一并生效）。
    try:
        _validate_runbook(data, name)
    except ValueError as exc:
        return tool_error(f"runbook 校验失败: {exc}")

    if exists and not overwrite:
        return tool_error(
            f"runbook 已存在: {name}（{path}）。需要覆盖请 overwrite=true。"
        )

    try:
        runbooks_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception as exc:
        return tool_error(f"写入 runbook 失败: {exc}")

    return json.dumps(
        {
            "status": "updated" if exists else "created",
            "name": name,
            "path": str(path),
            "steps": len(steps),
            "note": (
                "已创建/更新 runbook（schema v0.1）。runbook_load 可加载；"
                "若意图是行为约束，触发词已写入 triggers。"
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
    check_fn=check_runbook_requirements,
    emoji="📝",
    max_result_size_chars=30_000,
)
