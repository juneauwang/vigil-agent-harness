"""Runbook (程序层) tools for the Ops Agent Harness.

Implements the program-layer contract from ``ops-agent-harness.md`` §1 and
§3 L4:

  runbooks/<name>.yaml      — structured YAML: 触发条件 + 步骤 + 命令 + 回滚
                              (incident runbooks) and, for ``kind: deploy``
                              with ``checklist: true``, an L4 deployment
                              checklist (前置核对 → 滚动发布 → 真实验证 →
                              回滚预案) whose phase order is enforced by
                              ``runbook_checkpoint``.

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
_VALID_ENVS = {"test", "uat", "prod"}
_VALID_STATUSES = {"pass", "fail"}

_DEFAULT_LOAD_SCHEMA = {
    "name": "runbook_load",
    "description": (
        "加载运维 runbook（程序层：结构化 YAML，含触发条件 + 步骤 + 命令 + 回滚）。"
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
    return (home / _RUNBOOKS_DIRNAME / f"{name}.yaml").resolve()


def _state_path(home: Path) -> Path:
    return home / _RUNBOOKS_DIRNAME / _STATE_FILENAME


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
    if env is not None and str(env) not in _VALID_ENVS:
        raise ValueError(f"runbook {name} env 非法: {env!r}（可选 test/uat/prod）")
    for step in data["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str):
            raise ValueError(f"runbook {name} 的步骤缺少字符串 id")
        has_commands = isinstance(step.get("commands"), list) and bool(step["commands"])
        has_verify = isinstance(step.get("verify"), str)
        if not (has_commands or has_verify):
            raise ValueError(f"runbook {name} 步骤 {step.get('id')!r} 缺少 commands 或 verify")
    if data.get("checklist"):
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


def check_runbook_requirements() -> bool:
    """Runbook tools are gated on the ops runbooks being enabled in config.yaml."""
    return bool(_ops_config().get("runbooks", {}).get("enabled", False))


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
        "checklist": bool(rb.get("checklist")),
    }


def _full_payload(home: Path, rb: Dict[str, Any]) -> Dict[str, Any]:
    name = rb["name"]
    payload = dict(rb)
    payload["session_env"] = _session_env()
    if rb.get("env") and payload["session_env"] and rb["env"] != payload["session_env"]:
        payload["env_mismatch"] = True
        payload["env_warning"] = (
            f"runbook 适用环境 {rb['env']} 与当前会话环境 {payload['session_env']} 不一致——"
            "跨环境操作默认拒绝，确认后再执行。"
        )
    payload["checklist_state"] = _checklist_state_for(home, name) if rb.get("checklist") else None
    payload["note"] = (
        "本工具不执行任何命令；步骤命令由 agent 通过终端执行，逐条过权限矩阵。"
    )
    return payload


def runbook_load(
    runbook: Optional[str] = None,
    query: Optional[str] = None,
    home: Optional[Path] = None,
) -> str:
    """Load a runbook by name, fuzzy-match by trigger keywords, or list all."""
    home = home or _hermes_home()
    runbooks_dir = home / _RUNBOOKS_DIRNAME
    if not runbooks_dir.is_dir():
        return tool_error(
            f"runbooks 目录不存在: {runbooks_dir}。"
            "运维会话需要先铺 runbook（vigil ops-init 会复制样例到 ops profile）。"
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
    if not data.get("checklist"):
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
