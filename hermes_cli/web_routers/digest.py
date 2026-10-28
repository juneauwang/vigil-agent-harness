"""Le Registre（D3）：digest 发布配置 + 报告台账 + 生成流水线。

页面 `/registre` 的三块内容对应这里的端点：

  GET/PUT /api/digest/config       ops.digest.{window,publish.*}（令牌只回 configured + ref 名）
  PUT     /api/digest/credential   令牌写入 credential_vault（值不落日志、不回显）
  POST    /api/digest/test         用当前表单值 + vault 凭据打一次 BookStack GET /api/books/{id}
  GET     /api/digest/reports      <VIGIL_HOME>/reports/*.md 台账（只列元数据）
  GET     /api/digest/reports/{n}  单份 markdown（路径穿越防护）
  POST    /api/digest/generate     后台串行跑「写稿 job → 渲染 job」（fire_due 执行链）
  GET     /api/digest/runs         两条 digest job 最近的执行记录

安全红线：任何响应/日志都不回显令牌值；错误信息统一脱敏（vendor 报错也可能
带 token，BookstackTarget 已脱敏一层，这里再兜一层）。
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_cli.web_deps import late

_log = logging.getLogger("hermes_cli.web_server")

router = APIRouter()

_require_token = late("_require_token")
_api_error = late("_api_error")
_run_cron_dashboard_io = late("_run_cron_dashboard_io")
_list_cron_jobs_sync = late("_list_cron_jobs_sync")
_call_cron_for_profile = late("_call_cron_for_profile")
_fire_cron_job_for_profile = late("_fire_cron_job_for_profile")

DIGEST_SCRIPT = "builtin:digest"
DEFAULT_CREDENTIAL_REF = "bookstack-api"
PUBLISH_TARGETS = ("none", "file", "bookstack")
# 文件名前缀 → 粒度（与 ops.digest._PREFIX_BY_GRANULARITY 对齐）。
_GRANULARITY_BY_PREFIX = (
    ("Vigil-作业日报-", "daily"),
    ("Vigil-作业周报-", "weekly"),
    ("Vigil-作业报告-", "range"),
)

_WINDOW_RE = re.compile(r"^\d+[dhmw]$", re.IGNORECASE)
_CREDENTIAL_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _bad(code: str, message: str) -> JSONResponse:
    """400：同时给控制台的 error 信封和 FastAPI 风格 detail（前端两套解析都能读）。"""
    return JSONResponse(
        status_code=400,
        content={**_api_error(code, message), "detail": message},
    )


def _redact_detail(text: str, *secrets: str) -> str:
    """错误详情脱敏：先精确抹掉凭据值，再过全局 redact，最后截断。"""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    try:
        from agent.redact import redact_sensitive_text

        value = redact_sensitive_text(value, credential_values=True, force=True)
    except Exception:
        pass
    return value[:300]


def _reports_dir() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "reports"


def _digest_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    ops = cfg.get("ops") if isinstance(cfg, dict) else None
    digest = (ops or {}).get("digest") if isinstance(ops, dict) else None
    return digest if isinstance(digest, dict) else {}


def _credential_configured(ref: str) -> bool:
    """只问「有没有这个名字」，不读值（避免把明文带进请求上下文）。"""
    if not ref:
        return False
    try:
        from tools.credential_vault import list_names

        return ref in list_names()
    except Exception:
        return False


def _publish_snapshot() -> Dict[str, Any]:
    section = _digest_section()
    publish = section.get("publish") if isinstance(section.get("publish"), dict) else {}
    bookstack = publish.get("bookstack") if isinstance(publish.get("bookstack"), dict) else {}
    ref = str(bookstack.get("credential_ref") or DEFAULT_CREDENTIAL_REF)
    try:
        book_id = int(bookstack.get("book_id") or 0)
    except (TypeError, ValueError):
        book_id = 0
    return {
        "target": str(publish.get("target") or "none").strip().lower() or "none",
        "bookstack": {
            "base_url": str(bookstack.get("base_url") or "").strip(),
            "book_id": book_id,
            "credential_ref": ref,
            "configured": _credential_configured(ref),
        },
    }


def _config_payload() -> Dict[str, Any]:
    section = _digest_section()
    return {
        "window": str(section.get("window") or "").strip(),
        "publish": _publish_snapshot(),
    }


def _window_arg(args: Optional[List[Any]]) -> str:
    values = [str(item) for item in (args or [])]
    if "--window" in values:
        index = values.index("--window")
        if index + 1 < len(values):
            return values[index + 1]
    return ""


def _digest_jobs(jobs: List[Dict[str, Any]]) -> Tuple[Optional[dict], Optional[dict]]:
    """把 digest job 分成「写稿（facts）/ 渲染发布（md）」两条。"""
    facts: Optional[dict] = None
    render: Optional[dict] = None
    for job in jobs:
        if str(job.get("script") or "") != DIGEST_SCRIPT:
            continue
        args = [str(item) for item in (job.get("script_args") or [])]
        fmt = ""
        if "--format" in args:
            index = args.index("--format")
            if index + 1 < len(args):
                fmt = args[index + 1]
        if fmt == "facts" and facts is None:
            facts = job
        elif fmt == "md" and render is None:
            render = job
    return facts, render


def _granularity_of_name(name: str) -> str:
    for prefix, value in _GRANULARITY_BY_PREFIX:
        if name.startswith(prefix):
            return value
    return ""


def _posted_configured() -> bool:
    publish = _publish_snapshot()
    bookstack = publish["bookstack"]
    return (
        publish["target"] == "bookstack"
        and bool(bookstack["base_url"])
        and int(bookstack["book_id"] or 0) > 0
    )


def _publish_ledger() -> Dict[str, Any]:
    """发布账本 reports/publish-index.json（发布时写入的真实 URL）。"""
    try:
        from ops.digest import read_publish_index

        return read_publish_index(_reports_dir().parent)
    except Exception:
        return {}


def _list_reports_sync() -> List[Dict[str, Any]]:
    """台账：reports/*.md + 发布账本里的真实 URL。

    注意：URL 只能来自账本（发布时由 BookstackTarget 生成，形如
    ``/books/<book_slug>/page/<page_slug>``）。不要用 book_id 拼地址 ——
    BookStack 的 book 路由认 slug 不认 id，拼出来是死链。
    """
    directory = _reports_dir()
    items: List[Dict[str, Any]] = []
    ledger = _publish_ledger()
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            try:
                stat = path.stat()
            except OSError:
                continue
            # 账本键是「报告名」= 文件名去 .md（发布时 BookStack 页名同源），
            # 早期手写条目可能带 .md，两种都认。
            entry = ledger.get(path.stem) or ledger.get(path.name)
            entry = entry if isinstance(entry, dict) else {}
            url = str(entry.get("url") or "")
            items.append({
                "name": path.name,
                "path": str(path),
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "size": stat.st_size,
                "granularity": _granularity_of_name(path.name),
                "posted": bool(url),
                "url": url,
                "page_id": entry.get("page_id"),
            })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def _safe_report_path(name: str) -> Optional[Path]:
    """只允许 reports 目录下的 ``*.md``（拒绝分隔符/穿越）。"""
    raw = str(name or "").strip()
    if not raw or not raw.endswith(".md") or "/" in raw or "\\" in raw or raw in (".", ".."):
        return None
    root = _reports_dir().resolve()
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _runs_sync() -> List[Dict[str, Any]]:
    from cron.executions import list_executions

    jobs = _list_cron_jobs_sync("all")
    facts, render = _digest_jobs(jobs)
    names = {
        str(job.get("id")): str(job.get("name") or job.get("id"))
        for job in (facts, render)
        if job
    }
    rows: List[Dict[str, Any]] = []
    for job_id in names:
        rows.extend(list_executions(job_id=job_id, limit=5))
    rows.sort(key=lambda row: (str(row.get("claimed_at") or ""), str(row.get("id") or "")),
              reverse=True)
    out: List[Dict[str, Any]] = []
    for row in rows:
        started = str(row.get("started_at") or "")
        finished = str(row.get("finished_at") or "")
        duration: Optional[float] = None
        try:
            if started and finished:
                duration = round(
                    (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds(), 1
                )
        except ValueError:
            duration = None
        error = row.get("error")
        out.append({
            "id": str(row.get("id") or ""),
            "job_id": str(row.get("job_id") or ""),
            "job_name": names.get(str(row.get("job_id") or ""), ""),
            "status": str(row.get("status") or ""),
            "claimed_at": str(row.get("claimed_at") or ""),
            "started_at": started,
            "finished_at": finished,
            "duration_s": duration,
            "summary": _redact_detail(error)[:160] if error else "",
        })
    return out


def _generate_worker(facts: dict, render: dict) -> None:
    """后台串行：先写稿（facts），完成后再渲染发布（md）。"""
    for label, job in (("facts", facts), ("render", render)):
        try:
            fired = _fire_cron_job_for_profile(
                str(job.get("profile") or "default"), str(job.get("id") or "")
            )
            _log.info("digest generate: fired %s job %s -> %s", label, job.get("id"), fired)
        except Exception:
            _log.exception("digest generate: fire %s job %s failed", label, job.get("id"))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@router.get("/api/digest/config")
async def get_digest_config(request: Request):
    _require_token(request)
    return {"ok": True, "data": _config_payload()}


@router.put("/api/digest/config")
async def put_digest_config(
    payload: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
):
    """逐字段校验后只更新请求里出现的字段（非法 → 400 且什么都不写）。"""
    _require_token(request)
    body = payload or {}

    window: Optional[str] = None
    if "window" in body:
        window = str(body.get("window") or "").strip()
        if not _WINDOW_RE.fullmatch(window) or int(window[:-1]) <= 0:
            return _bad("invalid_window", "默认窗口需要形如 7d / 24h / 2w（且大于 0）")

    target: Optional[str] = None
    if "target" in body:
        target = str(body.get("target") or "").strip().lower()
        if target not in PUBLISH_TARGETS:
            return _bad("invalid_target", "发布目标必须是 none / file / bookstack")

    base_url: Optional[str] = None
    if "base_url" in body:
        base_url = str(body.get("base_url") or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            return _bad("invalid_base_url", "BookStack 地址需以 http(s):// 开头（或留空）")

    book_id: Optional[int] = None
    if "book_id" in body:
        raw_id = body.get("book_id")
        try:
            book_id = int(raw_id or 0)
        except (TypeError, ValueError):
            return _bad("invalid_book_id", "书 ID 必须是正整数（或 0 表示未设置）")
        if book_id < 0:
            return _bad("invalid_book_id", "书 ID 不能为负数")

    credential_ref: Optional[str] = None
    if "credential_ref" in body:
        credential_ref = str(body.get("credential_ref") or "").strip()
        if not _CREDENTIAL_REF_RE.fullmatch(credential_ref):
            return _bad(
                "invalid_credential_ref",
                "凭据名需匹配 ^[a-z0-9][a-z0-9._-]*$（如 bookstack-api）",
            )

    from hermes_cli.config import load_config, save_config

    try:
        cfg = load_config()
        digest = cfg.setdefault("ops", {}).setdefault("digest", {})
        if window is not None:
            digest["window"] = window
        if any(value is not None for value in (target, base_url, book_id, credential_ref)):
            publish = digest.setdefault("publish", {})
            if target is not None:
                publish["target"] = target
            if any(value is not None for value in (base_url, book_id, credential_ref)):
                bookstack = publish.setdefault("bookstack", {})
                if base_url is not None:
                    bookstack["base_url"] = base_url
                if book_id is not None:
                    bookstack["book_id"] = book_id
                if credential_ref is not None:
                    bookstack["credential_ref"] = credential_ref
        save_config(cfg)
    except Exception as exc:
        _log.exception("digest config save failed")
        return JSONResponse(
            status_code=500,
            content={**_api_error("config_save_failed", "写入 config.yaml 失败"),
                     "detail": "写入 config.yaml 失败"},
        )
    return {"ok": True, "data": _config_payload()}


# ---------------------------------------------------------------------------
# 凭据 / 连接测试
# ---------------------------------------------------------------------------

@router.put("/api/digest/credential")
async def put_digest_credential(
    payload: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
):
    """令牌写入 vault：值不落日志、不回显，只回 configured。"""
    _require_token(request)
    body = payload or {}
    ref = str(body.get("ref") or "").strip()
    if not _CREDENTIAL_REF_RE.fullmatch(ref):
        return _bad("invalid_credential_ref", "凭据名需匹配 ^[a-z0-9][a-z0-9._-]*$")
    value = body.get("value")
    if not isinstance(value, str) or not value.strip():
        return _bad("invalid_credential_value", "令牌不能为空")
    value = value.strip()
    if ":" not in value:
        return _bad(
            "invalid_credential_value",
            "令牌需要 BookStack 的 token_id:token_secret 形态（两段用冒号分隔）",
        )
    try:
        from tools.credential_vault import store

        store(ref, value, source="user")
    except Exception as exc:
        _log.warning("digest credential store failed for %r: %s", ref, type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={**_api_error("credential_store_failed", "写入凭据保险箱失败"),
                     "detail": _redact_detail(str(exc), value)},
        )
    _log.info("digest credential stored: %s", ref)
    return {"ok": True, "ref": ref, "configured": True}


def _status_from_message(message: str) -> Optional[int]:
    match = re.search(r"BookStack API (\d{3})", str(message or ""))
    return int(match.group(1)) if match else None


@router.post("/api/digest/test")
async def test_digest_publish(
    payload: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
):
    """用当前表单的 base_url/book_id + vault 凭据打一次 GET /api/books/{id}。"""
    _require_token(request)
    body = payload or {}
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    if not base_url or not base_url.startswith(("http://", "https://")):
        return _bad("invalid_base_url", "BookStack 地址需以 http(s):// 开头")
    try:
        book_id = int(body.get("book_id") or 0)
    except (TypeError, ValueError):
        return _bad("invalid_book_id", "书 ID 必须是正整数")
    if book_id <= 0:
        return _bad("invalid_book_id", "书 ID 必须是正整数")
    ref = str(body.get("credential_ref") or "").strip() or str(
        _publish_snapshot()["bookstack"]["credential_ref"]
    )
    if not _CREDENTIAL_REF_RE.fullmatch(ref):
        return _bad("invalid_credential_ref", "凭据名不合法")

    try:
        from tools.credential_vault import retrieve

        raw = retrieve(ref)
    except Exception:
        return {
            "ok": False,
            "status": None,
            "detail": f"读不到凭据 {ref}（先保存令牌，或检查 ops.digest.publish.bookstack.credential_ref）",
        }
    token_id, _, token_secret = raw.partition(":")
    if not token_id or not token_secret:
        return {"ok": False, "status": None, "detail": "凭据格式不是 token_id:token_secret"}

    def _call() -> Dict[str, Any]:
        from tools.publish_targets import BookstackTarget

        target = BookstackTarget(base_url, token_id, token_secret, timeout=10)
        return target._request("GET", f"/api/books/{book_id}")

    try:
        data = await _run_cron_dashboard_io(_call)
    except Exception as exc:
        return {
            "ok": False,
            "status": _status_from_message(str(exc)),
            "detail": _redact_detail(str(exc), token_secret, token_id, f"{token_id}:{token_secret}"),
        }
    name = str((data or {}).get("name") or "")
    return {
        "ok": True,
        "status": 200,
        "book_name": name,
        "book_id": int((data or {}).get("id") or book_id),
        "detail": f"已连接 BookStack：{name}" if name else "已连接 BookStack",
    }


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------

@router.get("/api/digest/reports")
async def list_digest_reports(request: Request):
    _require_token(request)
    reports = await _run_cron_dashboard_io(_list_reports_sync)
    return {"ok": True, "data": reports}


@router.get("/api/digest/reports/{name}")
async def get_digest_report(name: str, request: Request):
    _require_token(request)
    path = _safe_report_path(name)
    if path is None:
        raise HTTPException(status_code=400, detail="非法报告名（只允许 reports 目录下的 *.md）")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"报告不存在：{name}")
    try:
        content = path.read_text(encoding="utf-8")
        stat = path.stat()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取报告失败：{exc}") from exc
    return {
        "ok": True,
        "data": {
            "name": path.name,
            "content": content,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "size": stat.st_size,
            "granularity": _granularity_of_name(path.name),
        },
    }


# ---------------------------------------------------------------------------
# 生成流水线 / 执行记录
# ---------------------------------------------------------------------------

@router.post("/api/digest/generate")
async def generate_digest(
    payload: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
):
    """后台串行跑两条 digest job（facts → md），立刻返回 started。

    可选 ``window``：先把两条 job 的 ``--window`` 一起改掉（与「同步窗口」同一
    语义），再执行——避免生成的报告与流水线卡片显示的窗口不一致。
    """
    _require_token(request)
    body = payload or {}
    window = None
    if body.get("window") not in (None, ""):
        window = str(body.get("window") or "").strip()
        if not _WINDOW_RE.fullmatch(window) or int(window[:-1]) <= 0:
            return _bad("invalid_window", "窗口需要形如 7d / 24h / 1w（且大于 0）")

    jobs = await _run_cron_dashboard_io(_list_cron_jobs_sync, "all")
    facts, render = _digest_jobs(jobs or [])
    if facts is None or render is None:
        missing = [label for label, job in (("写稿（--format facts）", facts),
                                            ("渲染（--format md）", render)) if job is None]
        return JSONResponse(
            status_code=404,
            content={**_api_error(
                "digest_jobs_missing",
                "找不到 digest 流水线 job：" + "、".join(missing) + "（先从蓝图创建 Weekly work log）",
            ), "detail": "找不到 digest 流水线 job"},
        )

    if window:
        for job in (facts, render):
            args = _with_window(job.get("script_args"), window)
            try:
                await _run_cron_dashboard_io(
                    _call_cron_for_profile,
                    str(job.get("profile") or "default"),
                    "update_job",
                    str(job.get("id")),
                    {"script_args": args},
                )
            except Exception:
                _log.exception("digest generate: window sync failed for job %s", job.get("id"))
                return JSONResponse(
                    status_code=500,
                    content={**_api_error("window_sync_failed", "同步窗口失败"),
                             "detail": "同步窗口失败"},
                )

    threading.Thread(
        target=_generate_worker,
        args=(facts, render),
        name="digest-generate",
        daemon=True,
    ).start()
    return {
        "started": True,
        "jobs": [str(facts.get("id")), str(render.get("id"))],
        "window": window or _window_arg(facts.get("script_args")) or _window_arg(render.get("script_args")),
    }


def _with_window(args: Optional[List[Any]], window: str) -> List[str]:
    """只替换/插入 ``--window``，其余 script_args 原样保序（同前端 helper 语义）。"""
    base = [str(item) for item in (args or [])]
    out: List[str] = []
    index = 0
    replaced = False
    while index < len(base):
        if base[index] != "--window":
            out.append(base[index])
            index += 1
            continue
        has_value = index + 1 < len(base) and not base[index + 1].startswith("--")
        index += 2 if has_value else 1
        if not replaced:
            out.extend(["--window", window])
            replaced = True
    if not replaced:
        out = ["--window", window] + out
    return out


@router.get("/api/digest/runs")
async def list_digest_runs(request: Request):
    _require_token(request)
    runs = await _run_cron_dashboard_io(_runs_sync)
    return {"ok": True, "data": runs}
