"""BookStack 发布工具：``publish_bookstack``。

把一份 Markdown 页面发布到 BookStack：

* 给了 ``page_id`` → 直接 ``PUT /api/pages/{id}``；
* 否则先按 ``filter[name]`` 精确匹配 → 命中 ``PUT``（更新），未命中 ``POST``（新建）；
* 同一 ``name`` 重跑走更新，不会产生第二个页面（幂等）。

配置优先级（逐字段）：显式参数 > 环境变量 > ``<VIGIL_HOME>/credentials/bookstack.json``。
凭证文件必须是 0600（或更严格）权限才会被读取。工具**不设** ``requires_env``：
未配置凭证时它仍然出现在工具列表里，调用时才返回点名缺哪个变量的 ``tool_error``，
避免 cron 任务因为工具消失而静默失败。

令牌安全：日志、异常、返回值里一律不出现 token；异常文本先脱敏再抛出。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_constants import get_hermes_home
from tools.publish_targets import BookstackTarget, PublishTargetError
from tools.registry import registry, tool_error, tool_result

_CREDENTIALS_RELATIVE = Path("credentials") / "bookstack.json"
_MISSING_CRED_HELP = (
    "缺少 BookStack 配置：需要 BOOKSTACK_URL、BOOKSTACK_TOKEN_ID、"
    "BOOKSTACK_TOKEN_SECRET（或对应参数 base_url / token_id / token_secret），"
    "也可写入 {path}（chmod 600）。"
)


def _load_credentials_file(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """读取凭证文件；返回 (配置, 错误)。错误文本不含文件内容。"""
    if not path.exists():
        return {}, None
    if not path.is_file():
        return {}, f"凭证路径不是普通文件：{path}"
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return {}, f"无法读取凭证文件权限：{path}（{exc}）"
    if mode & 0o077:
        return {}, f"凭证文件权限过宽：{path}（当前 {oct(mode)}，需 chmod 600）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, f"凭证文件不是合法 JSON：{path}"
    if not isinstance(data, dict):
        return {}, f"凭证文件顶层必须是对象：{path}"
    return data, None


def _first(value: Any, *keys: str) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


def _resolve_config(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """按「参数 > 环境变量 > 凭证文件」解析配置。"""
    path = get_hermes_home() / _CREDENTIALS_RELATIVE
    file_cfg, file_error = _load_credentials_file(path)

    base_url = args.get("base_url") or os.environ.get("BOOKSTACK_URL") or _first(file_cfg, "base_url", "url")
    token_id = args.get("token_id") or os.environ.get("BOOKSTACK_TOKEN_ID") or _first(file_cfg, "token_id")
    token_secret = (
        args.get("token_secret")
        or os.environ.get("BOOKSTACK_TOKEN_SECRET")
        or _first(file_cfg, "token_secret", "token")
    )

    missing_env = []
    if not base_url:
        missing_env.append("BOOKSTACK_URL")
    if not token_id:
        missing_env.append("BOOKSTACK_TOKEN_ID")
    if not token_secret:
        missing_env.append("BOOKSTACK_TOKEN_SECRET")
    if missing_env:
        message = _MISSING_CRED_HELP.format(path=path)
        if file_error:
            message += f"（{file_error}）"
        return None, message

    return {
        "base_url": str(base_url).strip(),
        "token_id": str(token_id).strip(),
        "token_secret": str(token_secret).strip(),
        "book_id": args.get("book_id") or os.environ.get("BOOKSTACK_BOOK_ID") or _first(file_cfg, "book_id"),
    }, None


def _as_int(value: Any, field: str) -> Tuple[Optional[int], Optional[str]]:
    if value in (None, ""):
        return None, None
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, f"{field} 必须是整数"


def _handle_publish_bookstack(args: Dict[str, Any], **kwargs: Any) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_error("name 必填（页面名，通常是文件名去 .md）")
    markdown = args.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return tool_error("markdown 必填（页面正文）")

    config, config_error = _resolve_config(args)
    if config_error:
        return tool_error(config_error)

    book_id, err = _as_int(args.get("book_id") or config.get("book_id"), "book_id")
    if err:
        return tool_error(err)
    chapter_id, err = _as_int(args.get("chapter_id"), "chapter_id")
    if err:
        return tool_error(err)
    page_id, err = _as_int(args.get("page_id"), "page_id")
    if err:
        return tool_error(err)

    timeout = args.get("timeout", 30)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return tool_error("timeout 必须是数字")
    if timeout_value <= 0:
        return tool_error("timeout 必须大于 0")

    try:
        target = BookstackTarget(
            base_url=config["base_url"],
            token_id=config["token_id"],
            token_secret=config["token_secret"],
            timeout=timeout_value,
        )
        result = target.publish(
            name=name,
            markdown=markdown,
            book_id=book_id,
            chapter_id=chapter_id,
            page_id=page_id,
        )
    except PublishTargetError as exc:
        return tool_error(str(exc))
    except NotImplementedError as exc:
        return tool_error(str(exc))
    return tool_result(result)


PUBLISH_BOOKSTACK_SCHEMA = {
    "name": "publish_bookstack",
    "description": (
        "将 Markdown 页面发布到 BookStack。同名页面（同一本书内）自动更新，"
        "不存在时新建；返回 action、page_id 与页面 URL。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "页面名（= 文件名去 .md，例如 Vigil-周报-2026-W44）。",
            },
            "markdown": {
                "type": "string",
                "description": "页面正文（Markdown）。",
            },
            "book_id": {
                "type": "integer",
                "description": "目标书 ID。建页时必填（也可用环境变量 BOOKSTACK_BOOK_ID）。",
            },
            "chapter_id": {
                "type": "integer",
                "description": "可选：挂到指定章节（给了 chapter_id 时建页可不填 book_id）。",
            },
            "page_id": {
                "type": "integer",
                "description": "可选：直接更新指定页面（PUT /api/pages/{id}）。",
            },
            "base_url": {"type": "string", "description": "覆盖配置：BookStack 站点地址。"},
            "token_id": {"type": "string", "description": "覆盖配置：API Token ID。"},
            "token_secret": {"type": "string", "description": "覆盖配置：API Token Secret。"},
            "timeout": {"type": "number", "description": "可选：请求超时秒数，默认 30。"},
        },
        "required": ["name", "markdown"],
    },
}


registry.register(
    name="publish_bookstack",
    toolset="bookstack",
    schema=PUBLISH_BOOKSTACK_SCHEMA,
    handler=_handle_publish_bookstack,
    check_fn=None,
    requires_env=[],
    is_async=False,
    description="Publish a Markdown page to BookStack (create or update by name)",
    emoji="\U0001F4DA",
)
