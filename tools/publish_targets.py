"""发布目标抽象（publish_target）：file | bookstack | confluence。

周报链路的上层只关心「把一份 Markdown 发出去」，不关心落到哪里：

* ``file``       —— 写成本地 ``.md``（无网络，离线可用）。
* ``bookstack``  —— 调 BookStack REST API（``POST/PUT /api/pages``）。
* ``confluence`` —— 本轮只留接口，转换点见 ``ConfluenceTarget.publish``。

Confluence 的 storage format 是 XHTML，Markdown 不能直接提交；转换点应放在
``ConfluenceTarget._markdown_to_storage``。本轮不实现，也不改上层调用方式。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

import requests


class PublishTargetError(Exception):
    """发布失败（异常文本必须已脱敏，不得包含 token）。"""


class PublishTarget(ABC):
    """发布目标接口：同一份 Markdown，落到不同后端。"""

    name: str = ""

    @abstractmethod
    def publish(self, *, name: str, markdown: str, **options: Any) -> Dict[str, Any]:
        """发布并返回至少包含 ``success`` / ``action`` 的字典。"""


class FileTarget(PublishTarget):
    """本地文件发布：``<directory>/<name>.md``。"""

    name = "file"

    def __init__(self, directory: Path | str):
        self.directory = Path(directory).expanduser()

    def publish(self, *, name: str, markdown: str, **options: Any) -> Dict[str, Any]:
        filename = _safe_filename(name)
        path = self.directory / f"{filename}.md"
        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            raise PublishTargetError(f"写入文件失败: {exc}") from exc
        return {
            "success": True,
            "target": self.name,
            "action": "updated" if existed else "created",
            "path": str(path),
        }


class BookstackTarget(PublishTarget):
    """BookStack REST API 发布：按 name 精确匹配，命中则更新，否则新建。"""

    name = "bookstack"

    def __init__(
        self,
        base_url: str,
        token_id: str,
        token_secret: str,
        timeout: float = 30,
    ):
        if not base_url or not token_id or not token_secret:
            raise PublishTargetError("BookStack 配置不完整（base_url / token_id / token_secret）")
        if not timeout or float(timeout) <= 0:
            raise PublishTargetError("timeout 必须大于 0")
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self._token_id = str(token_id)
        self._token_secret = str(token_secret)
        self._token = f"{self._token_id}:{self._token_secret}"

    # -- 脱敏 ---------------------------------------------------------

    def _redact(self, text: Any) -> str:
        value = str(text or "")
        for secret in (self._token, self._token_secret, self._token_id):
            if secret:
                value = value.replace(secret, "***")
        value = re.sub(r"(?i)(Authorization:\s*Token)\s+\S+", r"\1 ***", value)
        value = re.sub(
            r"(?i)\b(token|secret|password|api[_-]?key)\b\s*[=:]\s*\S+",
            r"\1=***",
            value,
        )
        return value

    # -- HTTP ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Token {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise PublishTargetError(self._redact(f"请求 BookStack 失败: {exc}")) from exc

        if response.status_code >= 400:
            detail = self._redact((response.text or "")[:300])
            raise PublishTargetError(f"BookStack API {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublishTargetError("BookStack 返回内容不是 JSON") from exc
        if not isinstance(payload, dict):
            raise PublishTargetError("BookStack 返回 JSON 结构异常")
        return payload

    # -- 发布 ---------------------------------------------------------

    def publish(
        self,
        *,
        name: str,
        markdown: str,
        book_id: Optional[int] = None,
        chapter_id: Optional[int] = None,
        page_id: Optional[int] = None,
        **options: Any,
    ) -> Dict[str, Any]:
        if page_id:
            page = self._request(
                "PUT",
                f"/api/pages/{int(page_id)}",
                json={"name": name, "markdown": markdown},
            )
            action = "updated"
        else:
            if not book_id and not chapter_id:
                raise PublishTargetError("建页需要 book_id（或 chapter_id）")
            existing = self._find_page(name, book_id)
            if existing is not None:
                page_id = int(existing["id"])
                page = self._request(
                    "PUT",
                    f"/api/pages/{page_id}",
                    json={"name": name, "markdown": markdown},
                )
                action = "updated"
            else:
                payload: Dict[str, Any] = {"name": name, "markdown": markdown}
                if chapter_id:
                    payload["chapter_id"] = int(chapter_id)
                else:
                    payload["book_id"] = int(book_id)
                page = self._request("POST", "/api/pages", json=payload)
                action = "created"

        result_page_id = int(page.get("id") or page_id or 0)
        result_book_id = int(page.get("book_id") or book_id or 0) or None
        return {
            "success": True,
            "action": action,
            "page_id": result_page_id,
            "url": self._page_url(page, result_page_id, result_book_id),
            "book_id": result_book_id,
        }

    def _find_page(self, name: str, book_id: Optional[int]) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"filter[name]": name, "count": 50}
        if book_id:
            params["book_id"] = int(book_id)
        payload = self._request("GET", "/api/pages", params=params)
        items = payload.get("data")
        if not isinstance(items, list):
            return None
        matched = [item for item in items if isinstance(item, dict) and item.get("name") == name]
        if book_id:
            # 服务端 filter 可能被忽略；本地再按 book_id 收窄，避免跨书误更新。
            scoped = [item for item in matched if int(item.get("book_id") or 0) == int(book_id)]
            if scoped:
                matched = scoped
        return matched[0] if matched else None

    def _page_url(self, page: Dict[str, Any], page_id: int, book_id: Optional[int]) -> str:
        slug = str(page.get("slug") or "")
        if not slug and page_id:
            try:
                fetched = self._request("GET", f"/api/pages/{page_id}")
                slug = str(fetched.get("slug") or "")
                book_id = int(fetched.get("book_id") or book_id or 0) or book_id
            except PublishTargetError:
                slug = ""
        book_slug = self._book_slug(book_id)
        if book_slug and slug:
            return f"{self.base_url}/books/{book_slug}/page/{slug}"
        if slug:
            return f"{self.base_url}/books/{book_id or ''}/page/{slug}"
        return f"{self.base_url}/books/{book_id or ''}"

    def _book_slug(self, book_id: Optional[int]) -> str:
        if not book_id:
            return ""
        try:
            book = self._request("GET", f"/api/books/{int(book_id)}")
        except PublishTargetError:
            return ""
        return str(book.get("slug") or "")


class ConfluenceTarget(PublishTarget):
    """Confluence 占位实现（本轮不做）。

    转换点：Confluence 的存储格式是 XHTML（``<p>`` / ``<table>`` ...），
    不能直接提交 Markdown。实现时应把 ``markdown`` 经
    ``_markdown_to_storage`` 转成 storage format，再调用
    ``/rest/api/content/{id}`` 的 create/update 接口；本类对外接口保持不变。
    """

    name = "confluence"

    def __init__(self, base_url: str = "", token: str = "", timeout: float = 30):
        self.base_url = str(base_url or "").rstrip("/")
        self._token = str(token or "")
        self.timeout = float(timeout or 30)

    def _markdown_to_storage(self, markdown: str) -> str:  # pragma: no cover - 占位
        raise NotImplementedError("Confluence 转换未实现：Markdown -> storage format (XHTML)")

    def publish(self, *, name: str, markdown: str, **options: Any) -> Dict[str, Any]:
        raise NotImplementedError(
            "Confluence 发布未实现（本轮只预留接口）；"
            "转换点见 ConfluenceTarget._markdown_to_storage"
        )


def _safe_filename(name: str) -> str:
    """文件名只保留单层，防目录穿越；空名回退。"""
    cleaned = str(name or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = cleaned.replace("\x00", "")
    return cleaned or "untitled"


def build_target(kind: str, **options: Any) -> PublishTarget:
    """按名字构造发布目标；配置缺项由具体实现抛 PublishTargetError。"""
    normalized = str(kind or "").strip().lower()
    if normalized == "file":
        return FileTarget(options.get("directory") or ".")
    if normalized == "bookstack":
        return BookstackTarget(
            base_url=options.get("base_url", ""),
            token_id=options.get("token_id", ""),
            token_secret=options.get("token_secret", ""),
            timeout=options.get("timeout", 30),
        )
    if normalized == "confluence":
        return ConfluenceTarget(
            base_url=options.get("base_url", ""),
            token=options.get("token", ""),
            timeout=options.get("timeout", 30),
        )
    raise PublishTargetError(f"未知发布目标: {kind!r}（支持 file / bookstack / confluence）")


__all__ = [
    "PublishTarget",
    "PublishTargetError",
    "FileTarget",
    "BookstackTarget",
    "ConfluenceTarget",
    "build_target",
]
