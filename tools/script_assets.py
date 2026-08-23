"""YAPL P4 脚本资产库（§10.9 逃生舱受控 + §11.3 run_script 特例）。

run_script 只引用资产库脚本（``~/.vigil/scripts/<name>.sh``），不内联。脚本
资产创建/变更走**内容审批**：tirith 扫描（block → 拒绝；warn → 强制人工）+
``request_asset_approval``（复用 P3 资产审批门，approvals.mode 决定方式，
fail-closed 永不无人落盘）。审批通过落盘预审标记 ``.meta/<name>.json``
（approved_at / approved_by / approved_version=内容哈希）——执行豁免数据模型：
脚本内容被改动后哈希漂移 = 豁免失效（runbook_exec 执行时校验）。

安全：
- 资产名严格 kebab-case（防路径穿越）；内容拒绝空/超限；
- 凭据纪律：审批 description 只含脚本摘要（首行注释/行数），不含内容明文；
- 落盘 0700（脚本可执行、防他读）。
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_CONTENT_CHARS = 200_000


def _hermes_home() -> Path:
    from tools.runbook_tools import _hermes_home as _rb_home
    return _rb_home()


def scripts_dir(home: Optional[Path] = None) -> Path:
    return Path(home or _hermes_home()).resolve() / "scripts"


def meta_dir(home: Optional[Path] = None) -> Path:
    return scripts_dir(home) / ".meta"


def _stored_name(name: str) -> str:
    """资产名 → 落盘文件名：无后缀补 .sh（执行器解析兼容裸名/带后缀）。"""
    if Path(name).suffix in (".sh", ".bash"):
        return name
    return f"{name}.sh"


def resolve_script_path(home: Path, name: str) -> Optional[Path]:
    """资产引用 → 脚本路径（裸名/带后缀双形态；不含 traversal）。"""
    name = str(name or "").strip()
    if not name or re.search(r"[\\/]", name):
        return None
    base = scripts_dir(home)
    candidates = [base / name]
    if not Path(name).suffix:
        candidates += [base / f"{name}.sh", base / f"{name}.bash"]
    return next((p for p in candidates if p.is_file()), None)


def read_meta(home: Path, name: str) -> Optional[Dict[str, Any]]:
    """读取脚本资产审批标记（.meta/<落盘名>.json）。"""
    path = meta_dir(home) / f"{_stored_name(name)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _summary(content: str) -> str:
    """审批 description 用摘要：首行注释 + 行数（不含内容明文，凭据纪律）。"""
    lines = [ln for ln in content.splitlines() if ln.strip()]
    head = ""
    for ln in lines[:3]:
        if ln.lstrip().startswith("#"):
            head = ln.strip().lstrip("#").strip()
            break
    return f"{len(lines)} 行" + (f"，首行注释：{head[:60]}" if head else "")


def script_asset_create(
    name: str,
    content: str,
    description: str = "",
    overwrite: bool = False,
    home: Optional[Path] = None,
) -> str:
    """创建/更新脚本资产（~/.vigil/scripts/<name>.sh）——内容审批后落盘。

    流程：名称/内容校验 → tirith 扫描（block 拒绝 / warn 强制人工）→ 资产
    审批门（approvals.mode；fail-closed）→ 落盘脚本(0700) + .meta 预审标记。
    返回 JSON（含预审标记）；审批失败不落盘 + 报错引导。
    """
    home = Path(home or _hermes_home()).resolve()
    name = str(name or "").strip()
    if not _NAME_RE.match(name) or ".." in name:
        return tool_error(
            f"脚本资产名非法: {name!r}——必须 kebab-case（小写字母/数字/./_/-，"
            "不含 .. 路径穿越）"
        )
    content = str(content or "")
    if not content.strip():
        return tool_error("脚本资产 content 必填且不能为空")
    if len(content) > _MAX_CONTENT_CHARS:
        return tool_error(
            f"脚本资产 content 超限（{len(content)} > {_MAX_CONTENT_CHARS}）"
        )
    stored = _stored_name(name)
    script_path = scripts_dir(home) / stored
    existed = script_path.is_file()
    if existed and not overwrite:
        return tool_error(
            f"脚本资产已存在: {stored}（{script_path}）。需要覆盖请 overwrite=true"
            "（重新过内容审批）。"
        )

    # tirith 扫描（内容级安全，§11.4 prod 定时 script 严格度来源）。
    tirith_warn = False
    tirith_note = ""
    try:
        from tools.tirith_security import check_command_security
        scan = check_command_security(content)
        action = str(scan.get("action") or "allow")
        if action == "block":
            return tool_error(
                f"脚本资产内容被 tirith 拦截（block）："
                f"{scan.get('summary') or '安全扫描不通过'}"
                f"{'；findings: ' + json.dumps(scan.get('findings') or [], ensure_ascii=False) if scan.get('findings') else ''}"
                "——请修改脚本内容后重试"
            )
        if action == "warn":
            tirith_warn = True
            tirith_note = f"；tirith warn: {scan.get('summary') or '有安全提示'}"
    except Exception as exc:
        logger.warning("脚本资产 tirith 扫描失败（fail-open 继续）: %s", exc)

    # 资产审批门（§11.3 run_script 特例 + §11.4 内容审批）。
    from tools.approval import request_asset_approval
    result = request_asset_approval(
        asset_type="script",
        asset_name=name,
        description=(
            f"脚本资产 {name} 创建/变更内容审批——{_summary(content)}"
            f"{tirith_note or ''}"
        ),
        env="",
        force_manual=tirith_warn,
        smart_low_risk=False,  # 脚本内容本身是风险裁决——不走矩阵式 smart 自动批准
    )
    if not result.get("approved"):
        return tool_error(
            f"脚本资产审批未通过，未落盘: {result.get('message') or '拒绝'}——"
            "脚本内容未写入磁盘；请由用户在交互会话中创建。"
        )

    approved_version = _content_hash(content)
    meta = {
        "approved_at": _now_iso(),
        "approved_by": str(result.get("approved_by") or getpass.getuser()),
        "approved_version": approved_version,
        "description": description or "",
        "tirith": "warn" if tirith_warn else "allow",
    }
    try:
        base = scripts_dir(home)
        base.mkdir(parents=True, exist_ok=True)
        meta_dir(home).mkdir(parents=True, exist_ok=True)
        script_path.write_text(content, encoding="utf-8")
        script_path.chmod(0o700)
        (meta_dir(home) / f"{stored}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        return tool_error(f"脚本资产落盘失败: {exc}")
    return json.dumps({
        "status": "updated" if existed else "created",
        "name": name,
        "path": str(script_path),
        "meta": meta,
        "note": (
            "脚本资产已过内容审批落盘（预审标记 approved_at/approved_by/"
            "approved_version）。run_script 只引用资产名；执行器校验标记 + "
            "内容哈希（改后豁免失效需重新审批）。"
        ),
    }, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def script_asset_list(home: Optional[Path] = None) -> str:
    """列出脚本资产（名称/路径/审批状态/摘要）。"""
    home = Path(home or _hermes_home()).resolve()
    base = scripts_dir(home)
    if not base.is_dir():
        return json.dumps({"scripts": [], "note": "脚本资产库为空——"
                          "用 script_asset_create 创建（内容审批后落盘）"},
                          ensure_ascii=False, indent=2)
    rows: List[Dict[str, Any]] = []
    for p in sorted(base.glob("*.sh")) + sorted(base.glob("*.bash")):
        meta = read_meta(home, p.name)
        rows.append({
            "name": p.stem if p.suffix in (".sh", ".bash") else p.name,
            "path": str(p),
            "approved": bool(meta and meta.get("approved_at")),
            "approved_by": (meta or {}).get("approved_by", ""),
            "approved_at": (meta or {}).get("approved_at", ""),
        })
    return json.dumps({"scripts": rows, "count": len(rows)},
                      ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DEFAULT_CREATE_SCHEMA = {
    "name": "script_asset_create",
    "description": (
        "创建/更新脚本资产（~/.vigil/scripts/<name>.sh）——run_script 只引用资产"
        "（不内联脚本）的前提。创建/变更走内容审批：tirith 安全扫描（block 拒绝 / "
        "warn 强制人工）+ 资产审批门（approvals.mode 决定方式；approvals.mode=off "
        "且非 warn 可跳过；无人在场 fail-closed 不落盘）。审批通过落盘预审标记 "
        "approved_at/approved_by/approved_version（内容哈希）——P4 执行豁免数据"
        "模型：脚本被手改后哈希漂移，run_script 拒绝执行需重新审批。"
        "引用不存在脚本时用本工具创建（不是内联到 runbook）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "资产名（kebab-case，如 db-backup；可带 .sh）。",
            },
            "content": {
                "type": "string",
                "description": "脚本内容（bash 脚本；创建后 0700 可执行）。",
            },
            "description": {
                "type": "string",
                "description": "资产用途说明（审批展示用，不含凭据）。",
            },
            "overwrite": {
                "type": "boolean",
                "description": "同名已存在需 true（重新过内容审批）。",
            },
        },
        "required": ["name", "content"],
    },
}

_DEFAULT_LIST_SCHEMA = {
    "name": "script_asset_list",
    "description": (
        "列出脚本资产库（~/.vigil/scripts/）：名称/路径/审批状态/审批人——"
        "run_script 引用前确认资产存在且已过内容审批。只读。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def _create_handler(args: Dict[str, Any], **kwargs) -> str:
    return script_asset_create(
        name=args.get("name", ""),
        content=args.get("content", ""),
        description=args.get("description") or "",
        overwrite=bool(args.get("overwrite", False)),
    )


def _list_handler(args: Dict[str, Any], **kwargs) -> str:
    return script_asset_list()


# 顶层 registry.register（工具发现机制只认模块顶层调用——_register() 包装会被
# AST 扫描跳过，导致 CLI 运行时工具不加载；YAPL P1-3 曾踩此坑）。
registry.register(
    name="script_asset_create",
    toolset="runbook",
    schema=_DEFAULT_CREATE_SCHEMA,
    handler=_create_handler,
    check_fn=lambda: True,
    emoji="📜",
    max_result_size_chars=8_000,
)
registry.register(
    name="script_asset_list",
    toolset="runbook",
    schema=_DEFAULT_LIST_SCHEMA,
    handler=_list_handler,
    check_fn=lambda: True,
    emoji="🗂️",
    max_result_size_chars=8_000,
)
