#!/usr/bin/env python3
"""Vigil 作业日志（digest）：按「我提的任务 → 实际做的变更」汇总一个时间窗。

原 ``cron/scripts/oplog_digest.py`` 的采集/渲染逻辑搬到这里，作为随 wheel 分发的
内置能力；旧脚本保留为薄 shim。facts 字段名与抽取规则不变。

用法：
    vigil digest --window 7d --format facts
    vigil digest --window 2026-W43 --format md --out /tmp/r.md
    vigil digest --window 7d --format md --publish bookstack
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SHANGHAI = timezone(timedelta(hours=8))
CRON_HDR = re.compile(r"^\[IMPORTANT: You are running as a scheduled cron job")
MAX_ASK = 140
MAX_DETAIL = 160
MAX_CHANGES_PER_TASK = 40
MAX_OBJECTS_PER_TASK = 60
EXCLUDED_SOURCES = ("cron", "subagent")

DEFAULT_WINDOW = "7d"

# 报告粒度（D1）：窗口跨度 → 标题词 / 文件名前缀。label 里只允许
# ``[0-9A-Za-z_-]``——BookStack 会丢掉中文并拿 ASCII 做 slug，中文只出现在
# 标题与文件名前缀里，页名 = 文件名去扩展名（规则不变）。
_GRANULARITY_TITLE = {"daily": "日报", "weekly": "周报", "range": "报告"}
_PREFIX_BY_GRANULARITY = {
    "daily": "Vigil-作业日报-",
    "weekly": "Vigil-作业周报-",
    "range": "Vigil-作业报告-",
}
# 兼容：D1 之前所有产物都叫「作业周报」。新前缀读不到 prose 时回落读它
# （旧 job 产出的 prose 文件还在，不能因为改名而丢掉模型叙述）。
_LEGACY_WEEKLY_PREFIX = _PREFIX_BY_GRANULARITY["weekly"]

# 粒度阈值：≤2 天日报；2~10 天周报；>10 天区间报告。
_DAILY_MAX_SPAN = timedelta(days=2)
_WEEKLY_MAX_SPAN = timedelta(days=10)

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?\S+"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[=:]\s*\S+"),
]


class DigestPublishError(Exception):
    """发布失败（消息里不得含凭据）。"""


def redact(text: str) -> str:
    value = str(text or "")
    for pat in _SECRET_PATTERNS:
        value = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", value)
    return value


def clean(text: object, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return redact(value)[:limit]


def ask_line(text: str) -> str:
    """用户意图取第一句（避免把整段交接报告当成任务）。"""
    head = re.split(r"[\n。?!?!；;]", str(text or "").strip(), maxsplit=1)[0]
    return clean(head, 100)


# --------------------------------------------------------------------------
# 时间窗
# --------------------------------------------------------------------------

def parse_window(args, now: datetime) -> Tuple[datetime, datetime, str]:
    if args.week and (args.window or args.since or args.until):
        raise ValueError("--week 不能与 --window/--since/--until 同时使用")
    if args.since and args.window:
        raise ValueError("--since/--until 不能与 --window 同时使用")
    if args.week:
        m = re.fullmatch(r"(\d{4})-W(\d{1,2})", args.week.strip())
        if not m:
            raise ValueError("--week 需要形如 2026-W43")
        year, week = int(m.group(1)), int(m.group(2))
        try:
            monday = datetime.combine(
                date.fromisocalendar(year, week, 1), datetime.min.time(), SHANGHAI
            )
        except ValueError as exc:
            raise ValueError(f"非法 ISO 周：{args.week!r}") from exc
        return monday, monday + timedelta(days=7), f"{year}-W{week:02d}"
    if args.since:
        if not args.until:
            raise ValueError("--since 与 --until 必须同时提供")
        since = _parse_dt(args.since)
        until = _parse_dt(args.until)
        if since >= until:
            raise ValueError("--since 必须早于 --until")
        return since, until, label_for(since, until)
    m = re.fullmatch(r"(\d+)([dhmw])", (args.window or "7d").strip(), re.IGNORECASE)
    if not m:
        raise ValueError("--window 需要形如 7d / 24h / 2w")
    n, unit = int(m.group(1)), m.group(2).lower()
    if n <= 0:
        raise ValueError("--window 必须大于 0")
    delta = {
        "d": timedelta(days=n),
        "h": timedelta(hours=n),
        "m": timedelta(minutes=n),
        "w": timedelta(weeks=n),
    }[unit]
    since = now - delta
    return since, now, label_for(since, now)


def _parse_dt(text: str) -> datetime:
    text = str(text).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = datetime.combine(date.fromisoformat(text), datetime.min.time(), SHANGHAI)
        return parsed
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def granularity(span: timedelta) -> str:
    """窗口跨度 → 粒度：``daily``（≤2 天）/ ``weekly``（≤10 天）/ ``range``。"""
    if span <= _DAILY_MAX_SPAN:
        return "daily"
    if span <= _WEEKLY_MAX_SPAN:
        return "weekly"
    return "range"


def report_prefix(span: timedelta) -> str:
    """报告文件名前缀（页名 = 文件名去扩展名，中文只在这个前缀里）。"""
    return _PREFIX_BY_GRANULARITY[granularity(span)]


def label_for(since: datetime, until: datetime, week: Optional[str] = None) -> str:
    """窗口 → label（时区固定 Asia/Shanghai，终点用 ``until - 1s`` 取日期）。

    - ``--week`` 给定时用它本身（保持现状 ``YYYY-Www``）；
    - 跨度 ≤2 天 → ``YYYY-MM-DD``；
    - 2 天 < 跨度 ≤10 天 → ISO 周 ``YYYY-Www``；
    - >10 天 → ``YYYY-MM-DD_YYYY-MM-DD``（since ~ until-1s）。

    label 只含 ``[0-9A-Za-z_-]``：BookStack 拿 ASCII 做 slug，中文放标题与前缀。
    """
    if week:
        return str(week)
    end = (until - timedelta(seconds=1)).astimezone(SHANGHAI)
    kind = granularity(until - since)
    if kind == "daily":
        return end.date().isoformat()
    if kind == "weekly":
        iso = end.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    start = since.astimezone(SHANGHAI)
    return f"{start.date().isoformat()}_{end.date().isoformat()}"


# --------------------------------------------------------------------------
# 变更项抽取
# --------------------------------------------------------------------------

def _json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _fmt_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return clean(v, 48)


def change_from_call(name: str, raw) -> Optional[dict]:
    """把一次写类工具调用折成一条变更记录。"""
    a = _json(raw)
    if name == "topo_update":
        updates = a.get("updates") or {}
        detail = "; ".join(f"{k}={_fmt_value(v)}" for k, v in updates.items()) or "（无字段）"
        return {
            "kind": "拓扑",
            "target": a.get("entity") or "?",
            "detail": clean(detail, MAX_DETAIL),
            "why": clean(a.get("reason"), 100),
        }
    if name in ("write_file", "patch"):
        path = a.get("path") or a.get("file_path") or "?"
        action = "改" if name == "patch" else "写"
        head = a.get("new_string") if name == "patch" else a.get("content")
        return {
            "kind": "文件",
            "target": path,
            "detail": f"{action}：{purpose_of(head, path)}",
            "why": "",
        }
    if name == "runbook_execute":
        ctx = a.get("trigger_context") or {}
        return {
            "kind": "runbook",
            "target": f"{a.get('runbook') or '?'}",
            "detail": f"env={a.get('env') or '-'}",
            "why": clean(ctx.get("reason"), 100),
        }
    if name == "skill_manage":
        target = a.get("name") or "?"
        detail = " ".join(x for x in (a.get("action"), a.get("file_path")) if x) or "patch"
        return {"kind": "技能", "target": target, "detail": detail, "why": ""}
    if name == "memory":
        ops = a.get("operations") or []
        heads = [f"{o.get('action')}:{clean(o.get('content'), 40)}" for o in ops[:2] if isinstance(o, dict)]
        return {"kind": "记忆", "target": a.get("target") or "-", "detail": "; ".join(heads) or "update", "why": ""}
    if name == "publish_bookstack":
        return {"kind": "发布", "target": a.get("name") or "?", "detail": "BookStack 页面", "why": ""}
    return None


# --------------------------------------------------------------------------
# 对象抽取：报告只写「集群里的对象变了什么」，工具调用只作证据
#
# 原则：能从命令/脚本内容/接口返回里**确定性**抽到的标识才写；抽不到就留空，
# 渲染成「标识未记录」并计入覆盖率缺口——禁止让模型补全。
# --------------------------------------------------------------------------

# 对象类型 → 子目标族（族决定分组，类型决定表格「对象」列）
_OBJ_FAMILY = {
    "账号与权限": "账号",
    "监控采集": "监控",
    "监控面板": "监控",
    "数据源": "监控",
    "配置": "配置",
    "系统服务": "配置",
    "拓扑": "拓扑",
    "可复用资产": "交付",
    "技能": "交付",
    "文档": "交付",
    "发布": "交付",
}
_FAMILY_LABEL = {"账号": "数据库账号", "监控": "监控接入", "拓扑": "拓扑实体", "配置": "配置与服务", "交付": "交付与资产"}
_FAMILY_ORDER = ("账号", "监控", "拓扑", "配置", "交付")
MAX_ROWS_PER_INTENT = 15

# 过程产物：临时脚本/诊断/验证文件——只做证据，不进报告
_RE_PROCESS_PATH = re.compile(
    r"(?i)(?:^|/)(?:tmp|temp)/|(?:^|/)(?:diag|probe|smoke)[-_]|[-_](?:verify|check)\.[a-z]+$"
)

_RE_ROLE = re.compile(r"(?i)\bcreate\s+(?:role|user)\s+\"?([a-z_][\w$@.-]*)\"?")
_RE_JOB_NAME = re.compile(r"(?i)\bjob_name\s*[:=]\s*[\"']?([\w.-]+)")
_RE_SCRAPE_INT = re.compile(r"(?i)\bscrape_interval\s*[:=]\s*[\"']?([\w.]+)")
_RE_TARGETS = re.compile(r"(?i)\btargets\s*:\s*\[([^\]]{0,160})\]")
_RE_UID = re.compile(r"(?i)\"uid\"\s*:\s*\"([\w-]{4,})\"")
_RE_JSON_NAME = re.compile(r"(?i)\"name\"\s*:\s*\"([^\"]{1,60})\"")
_RE_URL = re.compile(r"https?://[\w.:@%/?=&~+-]{4,200}")
_RE_KV_PATH = re.compile(r"\b(?:kv|secret|openbao|vault)/[\w./-]{2,80}")
_RE_DB_HOST = re.compile(r"(?i)(?:-h|--host)[\s=]+([\w.-]+)")
_RE_DB_NAME = re.compile(r"(?i)(?:\s-d|--dbname|--database)[\s=]+([\w.-]+)")
_RE_SYSTEMD_UNIT = re.compile(r"(?i)^systemctl\s+(?:start|restart|enable|reload)\s+([\w@.:-]+)")
_RE_ANSIBLE = re.compile(r"(?i)\bansible-playbook\s+(\S+)")
_RE_KUBECTL_APPLY = re.compile(r"(?i)^kubectl\s+(apply|create|patch|delete)\b")
_RE_KUBECTL_FILE = re.compile(r"(?i)-f\s+(\S+)")
_RE_PKG_INSTALL = re.compile(r"(?i)\b(?:apt(?:-get)?|yum|dnf|apk)\s+(?:install|add)\s+(?:-\S+\s+)*([\w.+-]+)")
_RE_HELM = re.compile(r"(?i)\bhelm\s+(?:install|upgrade)\s+([\w.-]+)")
_RE_DOCKER_IMAGE = re.compile(r"(?i)\bdocker\s+run\b[^|;]*?\s([\w./-]+:[\w.-]+)")
_DB_CLIENTS = ("psql", "mysql", "mariadb", "mongosh", "redis-cli", "clickhouse-client", "sqlcmd")

_RESULT_HINTS = (
    (re.compile(r"(?i)\bstate[\"'=:\s]+up\b"), "target state=up"),
    (re.compile(r"(?i)\"health\"\s*:\s*\"?up"), "health=up"),
    (re.compile(r"(?i)\bselect\s+1\b"), "SELECT 1 通过"),
    (re.compile(r"(?i)\bHTTP/\d(?:\.\d)?\"?\s+2\d\d\b"), "HTTP 2xx"),
    (re.compile(r"(?i)\b(?:200|201|204)\b"), "HTTP 2xx"),
    (re.compile(r"(?i)\bcreated\b"), "created"),
    (re.compile(r"(?i)\bconfigured\b"), "configured"),
)


def _g(pat: re.Pattern, text: str, default: str = "") -> str:
    """取第一个捕获组；正则没有分组时取整个匹配。"""
    m = pat.search(str(text or ""))
    if not m:
        return default
    if pat.groups:
        return (m.group(1) or "").strip()
    return (m.group(0) or "").strip()


def _result_summary(text: str) -> str:
    """从工具输出里取一句可复核的结果；认不出返回空串（不猜）。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return ""
    for pat, label in _RESULT_HINTS:
        if pat.search(body):
            return label
    return ""


def _obj(type_: str, identity: str = "", params: str = "", result: str = "", source: str = "") -> dict:
    return {
        "type": type_,
        "identity": clean(identity, 90),
        "params": clean(params, 120),
        "result": clean(result, 60),
        "source": source,
    }


def _object_from_command(cmd: str, result: str, source: str, host: str = "") -> Optional[dict]:
    """从一条终端/提权命令认出「它对集群里的哪个对象做了什么」。"""
    flat = re.sub(r"\s+", " ", str(cmd or "")).strip()
    if not flat:
        return None
    low = flat.lower()
    where_host = host or _g(_RE_DB_HOST, flat)

    role = _g(_RE_ROLE, flat)
    if role and any(c in low for c in _DB_CLIENTS):
        db = _g(_RE_DB_NAME, flat)
        where = "/".join(x for x in (where_host, db) if x)
        params = "；".join(x for x in (
            f"role={role}",
            "SUPERUSER" if re.search(r"(?i)\bsuperuser\b", flat) else "",
        ) if x)
        return _obj("账号与权限", f"{role} @ {where}" if where else role,
                    params, _result_summary(result), source)

    if "curl" in low:
        url = _g(_RE_URL, flat)
        http_write = bool(re.search(
            r"(?i)(-X\s*(?:POST|PUT|PATCH|DELETE)|--data\b|(?<![A-Za-z])-d\s|--upload-file)", flat))
        if "/api/datasources" in low:
            if not http_write:
                return None  # GET 列表/查询不算变更
            uid = _g(_RE_UID, result)
            name = _g(_RE_JSON_NAME, result)
            ident = f"{name}（uid {uid}）" if name and uid else (name or (f"uid {uid}" if uid else ""))
            ds_url = _g(_RE_URL, result) or url
            return _obj("数据源", ident, ds_url, _result_summary(result), source)
        if "/api/dashboards" in low or "/d/" in low:
            if not http_write:
                return None
            uid = _g(_RE_UID, result) or _g(re.compile(r"/d/([\w-]{4,})"), url)
            return _obj("监控面板", f"dashboard {uid}" if uid else "", url,
                        _result_summary(result), source)
        if "/-/reload" in low:
            return None  # 热加载是证据，不是对象变更（结果会并到同族的 scrape job 上）
        if ("prometheus" in low or "/api/v1/" in low) and http_write:
            job = _g(_RE_JOB_NAME, flat)
            host_hint = re.sub(r"^https?://", "", url).split("/")[0] or where_host
            return _obj("监控采集",
                        f"scrape job {job}" if job else (f"Prometheus {host_hint}" if host_hint else "Prometheus"),
                        url, _result_summary(result), source)
        return None

    if re.search(r"(?i)openbao|vault\b", low) and re.search(r"(?i)\b(kv|secret)/", low):
        return _obj("账号与权限", _g(_RE_KV_PATH, flat), "口令写入密钥库",
                    _result_summary(result), source)

    playbook = _g(_RE_ANSIBLE, flat)
    if playbook and not re.search(r"(?i)--check|--syntax-check", flat):
        return _obj("配置", Path(playbook).name, f"playbook 执行 @ {where_host}" if where_host else "playbook 执行",
                    _result_summary(result), source)
    if _RE_KUBECTL_APPLY.search(low):
        target_file = _g(_RE_KUBECTL_FILE, flat)
        ident = Path(target_file).name if target_file else flat[:60]
        return _obj("配置", ident,
                    f"kubectl {_RE_KUBECTL_APPLY.search(low).group(1)}" + (f" @ {where_host}" if where_host else ""),
                    _result_summary(result), source)
    pkg = _g(_RE_PKG_INSTALL, flat)
    if pkg:
        return _obj("系统服务", f"{pkg} @ {where_host}" if where_host else pkg,
                    "包安装", _result_summary(result), source)
    helm = _g(_RE_HELM, flat)
    if helm:
        return _obj("配置", f"helm release {helm}", f"@ {where_host}" if where_host else "",
                    _result_summary(result), source)
    image = _g(_RE_DOCKER_IMAGE, flat)
    if image:
        return _obj("系统服务", f"容器 {image}", f"@ {where_host}" if where_host else "",
                    _result_summary(result), source)
    unit = _g(_RE_SYSTEMD_UNIT, flat)
    if unit:
        return _obj("系统服务", unit, "systemctl" + (f" @ {where_host}" if where_host else ""),
                    _result_summary(result), source)
    return None


def _objects_from_content(path: str, content: str, result: str, source: str) -> List[dict]:
    """从写文件/改文件的内容里认出对象（prometheus job、账号、数据源、面板、配置、文档）。"""
    if not content or not str(content).strip():
        return []
    if _RE_PROCESS_PATH.search(str(path or "")):
        return []  # 过程脚本：不计入
    out: List[dict] = []

    job = _g(_RE_JOB_NAME, content)
    if job:
        params = "；".join(x for x in (
            f"targets=[{_g(_RE_TARGETS, content)}]" if _g(_RE_TARGETS, content) else "",
            f"interval={_g(_RE_SCRAPE_INT, content)}" if _g(_RE_SCRAPE_INT, content) else "",
        ) if x)
        out.append(_obj("监控采集", f"scrape job {job}", params, _result_summary(result), source))

    role = _g(_RE_ROLE, content)
    if role:
        out.append(_obj("账号与权限", role, f"role={role}；由 SQL/playbook 定义",
                        _result_summary(result), source))

    kv = _g(_RE_KV_PATH, content)
    if kv and role:
        out.append(_obj("账号与权限", f"口令 {kv}", f"role={role}；口令写入 {kv}",
                        _result_summary(result), source))

    if re.search(r"(?i)datasource", content) and _RE_UID.search(content):
        name = _g(_RE_JSON_NAME, content)
        uid = _g(_RE_UID, content)
        out.append(_obj("数据源", f"{name}（uid {uid}）" if name else f"uid {uid}",
                        _g(_RE_URL, content), _result_summary(result), source))

    if re.search(r"(?i)\"panels\"|dashboard", content) and not out:
        uid = _g(_RE_UID, content)
        out.append(_obj("监控面板", f"dashboard {uid}" if uid else "",
                        _g(_RE_URL, content), _result_summary(result), source))

    if out:
        return out

    base = Path(str(path or "")).name
    if base.endswith(".md"):
        return [_obj("文档", base, "", _result_summary(result), source)]
    if "/runbooks/" in str(path or "") and base.endswith((".yml", ".yaml")):
        return [_obj("可复用资产", f"runbook {Path(base).stem}", "", _result_summary(result), source)]
    if base.endswith((".yml", ".yaml", ".json", ".toml", ".service", ".timer", ".conf", ".ini")):
        return [_obj("配置", base, "", _result_summary(result), source)]
    return []


def objects_from_call(name: str, args: dict, result: str, source: str) -> List[dict]:
    """一次工具调用 → 0..N 个变更对象。认不出的返回空（过程动作不进报告）。"""
    if name in ("terminal", "sudo_exec", "vssh"):
        host = str(args.get("host") or "")
        obj = _object_from_command(str(args.get("command") or ""), result, source, host)
        return [obj] if obj else []
    if name == "topo_delete":
        ent = str(args.get("entity") or "")
        if not ent:
            return []
        env = archived = ""
        try:
            payload = json.loads(str(result or "{}"))
        except Exception:
            payload = {}
        if str(payload.get("status") or "") != "deleted":
            return []  # 被引用拦住 / 审批拒绝：没删成功，不记变更
        env = str(payload.get("env") or "")
        archived = str(payload.get("archived") or "")
        params = "；".join(x for x in (
            f"env={env}" if env else "",
            f"归档 {archived}" if archived else "",
            "force" if args.get("force") else "",
        ) if x)
        return [_obj("拓扑", ent, params, "删除（已归档）", source)]
    if name == "topo_discover":
        if not args.get("commit"):
            return []  # 只出草案不落盘：不算变更
        try:
            payload = json.loads(str(result or "{}"))
        except Exception:
            payload = {}
        drafts = [str(x).strip() for x in (payload.get("drafts") or []) if str(x).strip()]
        return [_obj("拓扑", d, "", "新增（待审）", source) for d in drafts[:20]]
    if name == "topo_update":
        ent = str(args.get("entity") or "")
        if not ent:
            return []
        ups = args.get("updates") or {}
        parts = []
        for key, value in list(ups.items())[:4]:
            if key == "needs_review":
                continue  # 审核标记不是「改了什么」，单独落结果列
            if isinstance(value, (dict, list, tuple)):
                parts.append(f"{key}=[…]")
            else:
                parts.append(f"{key}={_fmt_value(value)}")
        only_review = list(ups.keys()) == ["needs_review"] and ups.get("needs_review") is False
        hint = "审核通过" if only_review else "更新"
        return [_obj("拓扑", ent, "；".join(parts), hint, source)]
    if name in ("write_file", "patch"):
        path = str(args.get("path") or args.get("file_path") or "")
        body = args.get("new_string") if name == "patch" else args.get("content")
        return _objects_from_content(path, str(body or ""), result, source)
    if name == "runbook_execute":
        rb = str(args.get("runbook") or "")
        if not rb:
            return []
        return [_obj("可复用资产", f"runbook {rb}", f"env={args.get('env') or '-'}",
                     _result_summary(result), source)]
    if name == "script_asset_create":
        nm = str(args.get("name") or "")
        return [_obj("可复用资产", f"脚本资产 {nm}", "内容审批通过",
                     _result_summary(result), source)] if nm else []
    if name == "skill_manage":
        nm = str(args.get("name") or "")
        return [_obj("技能", nm, str(args.get("action") or "patch"),
                     _result_summary(result), source)] if nm else []
    if name == "publish_bookstack":
        nm = str(args.get("name") or "")
        return [_obj("发布", nm, "BookStack 页面", _result_summary(result), source)] if nm else []
    return []


_RE_ROLE_PARAM = re.compile(r"role=([\w$@.-]+)")


def _coalesce_accounts(objects: List[dict]) -> List[dict]:
    """同一个 role 的多条记录（psql 命令 / playbook / 口令路径）合成一个对象。

    报告里「对象」应该是一个账号，而不是三条不同来源的记录。
    """
    out: List[dict] = []
    by_role: Dict[str, dict] = {}
    for obj in objects:
        if obj["type"] != "账号与权限":
            out.append(obj)
            continue
        role = _g(_RE_ROLE_PARAM, obj["params"])
        if not role:
            out.append(obj)
            continue
        keeper = by_role.get(role)
        if keeper is None:
            by_role[role] = obj
            out.append(obj)
            continue
        if "@" in obj["identity"] and "@" not in keeper["identity"]:
            keeper["identity"] = obj["identity"]
        parts = [p for p in keeper["params"].split("；") if p and not p.startswith("role=")]
        for part in obj["params"].split("；"):
            if not part or part.startswith("role=") or part in parts:
                continue
            if part.startswith("口令写入"):
                parts = [x for x in parts if not x.startswith("口令写入")]
            parts.append(part)
        keeper["params"] = "；".join(parts)
        keeper["hits"] = int(keeper.get("hits") or 1) + int(obj.get("hits") or 1)
        if not keeper["result"]:
            keeper["result"] = obj["result"]
    return out


def _coalesce_configs(objects: List[dict]) -> List[dict]:
    """同一个文件（写的 + apply 的）合成一个对象：按文件名去重。"""
    out: List[dict] = []
    by_name: Dict[str, dict] = {}
    for obj in objects:
        if obj["type"] not in ("配置", "系统服务"):
            out.append(obj)
            continue
        base = Path(str(obj["identity"]).split(" ")[0]).name
        if not base or "." not in base:
            out.append(obj)
            continue
        keeper = by_name.get(base)
        if keeper is None:
            by_name[base] = obj
            out.append(obj)
            continue
        parts = [p for p in keeper["params"].split("；") if p]
        for part in obj["params"].split("；"):
            if part and part not in parts:
                parts.append(part)
        keeper["params"] = "；".join(parts)
        keeper["hits"] = int(keeper.get("hits") or 1) + int(obj.get("hits") or 1)
        if not keeper["result"]:
            keeper["result"] = obj["result"]
    return out


def _coalesce_topo(objects: List[dict]) -> List[dict]:
    """同一个拓扑实体被更新多次 → 合成一个对象（字段后写覆盖先写）。"""
    out: List[dict] = []
    by_ent: Dict[str, dict] = {}
    for obj in objects:
        if obj["type"] != "拓扑":
            out.append(obj)
            continue
        keeper = by_ent.get(obj["identity"])
        if keeper is None:
            by_ent[obj["identity"]] = obj
            out.append(obj)
            continue
        merged: Dict[str, str] = {}
        for part in (keeper["params"].split("；") + obj["params"].split("；")):
            if not part.strip():
                continue
            key, _, value = part.partition("=")
            merged[key] = value
        keeper["params"] = "；".join(f"{k}={v}" if v else k for k, v in merged.items())
        keeper["hits"] = int(keeper.get("hits") or 1) + int(obj.get("hits") or 1)
        results = [r for r in (keeper["result"], obj["result"]) if r]
        keeper["result"] = "；".join(dict.fromkeys(results))[:40]
    return out


def _coalesce_monitor(objects: List[dict]) -> List[dict]:
    """同一个 Prometheus 上的多次读写合成一个对象（保留最多 3 个不同 URL）。"""
    out: List[dict] = []
    by_id: Dict[str, dict] = {}
    for obj in objects:
        if obj["type"] != "监控采集":
            out.append(obj)
            continue
        keeper = by_id.get(obj["identity"])
        if keeper is None:
            by_id[obj["identity"]] = obj
            out.append(obj)
            continue
        urls = [p for p in keeper["params"].split("；") if p]
        if obj["params"] and obj["params"] not in urls and len(urls) < 3:
            urls.append(obj["params"])
        keeper["params"] = "；".join(urls)
        keeper["hits"] = int(keeper.get("hits") or 1) + int(obj.get("hits") or 1)
        if not keeper["result"]:
            keeper["result"] = obj["result"]
    return out


def _fill_monitor_results(objects: List[dict], extra: str = "") -> None:
    """热加载/查询的验证结果补到同任务的 scrape job 行上（只在监控采集内传播）。"""
    results = [o["result"] for o in objects if o["type"] == "监控采集" and o["result"]]
    if extra:
        results.append(extra)
    if not results:
        return
    for obj in objects:
        if obj["type"] == "监控采集" and not obj["result"]:
            obj["result"] = results[0]


_GENERIC_TOKENS = {
    "runbook", "scrape", "job", "http", "https", "www", "yml", "yaml", "json", "md",
    "env", "test", "prod", "the", "and", "for", "with", "scr", "tmp",
}


def _tokens(text: str) -> set:
    """标识里能用来判断「这件交付物是为谁做的」的词（粗粒度，够用即止）。"""
    parts = re.split(r"[^\w\u4e00-\u9fff]+", str(text or "").lower())
    return {p for p in parts if len(p) >= 3 and p not in _GENERIC_TOKENS}


def group_into_intents(objects: List[dict], ask: str = "") -> List[dict]:
    """把对象聚成「子目标」：账号 / 监控 / 配置 / 交付。

    交付物（runbook/技能/文档/发布）不单独成节：先按标识词与主族对齐
    （`xinggh-superadmin-account` → 账号族，`grafana-dashboard-sync` → 监控族），
    对不上就挂到它前面最近的那个主族；仍无则挂本任务的主族。
    """
    if not objects:
        return []
    buckets: Dict[str, List[dict]] = {}
    for obj in objects:
        fam = _OBJ_FAMILY.get(obj["type"], "交付")
        if fam != "交付":
            buckets.setdefault(fam, []).append(obj)
    dominant = max(buckets, key=lambda f: len(buckets[f]), default="")
    pools = {
        fam: set().union(*[_tokens(o["identity"]) | _tokens(o["params"]) for o in items])
        for fam, items in buckets.items()
    }

    last_family = ""
    for obj in objects:
        fam = _OBJ_FAMILY.get(obj["type"], "交付")
        if fam != "交付":
            last_family = fam
            continue
        toks = _tokens(obj["identity"]) | _tokens(obj["params"])
        scored = sorted(((len(toks & pools[f]), f) for f in pools), reverse=True)
        if scored and scored[0][0] > 0:
            target = scored[0][1]
        else:
            target = last_family or dominant or "交付"
        buckets.setdefault(target, []).append(obj)

    intents = []
    for fam in _FAMILY_ORDER:
        items = buckets.get(fam) or []
        if not items:
            continue
        ids = list(dict.fromkeys(
            o["identity"] for o in items
            if o["identity"] and o["type"] not in ("文档", "技能")
        ))
        tail = "、".join(ids[:2])
        label = _FAMILY_LABEL.get(fam, fam)
        if tail:
            label = f"{label}：{tail}"
        intents.append({
            "family": fam,
            "label": label,
            "objects": items,
            "with_identity": sum(1 for o in items if o["identity"]),
            "results": [o["result"] for o in items if o["result"]],
        })
    return intents


# 段首可剥的前缀（命令链被拆段后常见）
_LEAD_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:(?:sudo|doas|nohup|time|exec|command|do|then|else|if)\s+)+"
)
# 段首的变量赋值（`IMG=xxx kubectl ...`、`BK=$(date) cp ...`）
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# 必须在段首（剥掉前缀后）才算数的动作——避免 grep 'scp|ssh' 这类文本误命中
_LEADING_WRITE_RE = re.compile(
    r"(?i)^(?P<lead>"
    r"kubectl\s+(?P<kubectl>apply|delete|create|edit|scale|rollout|patch|drain|cordon|taint|label|annotate)"
    r"|helm\s+(?P<helm>install|upgrade|uninstall|rollback)"
    r"|systemctl\s+(?P<systemctl>start|stop|restart|reload|enable|disable|mask|unmask)"
    r"|docker(?:-compose|\s+compose)\s+(?P<compose>up|down|restart|start|stop|rm)"
    r"|docker\s+(?P<docker>run|rm|stop|start|restart|build|push|exec)"
    r"|(?P<scp>scp|rsync)"
    r"|(?P<fileop>rm|mv|cp|chmod|chown|mkfs|truncate|dd)"
    r"|git\s+(?P<git>push|reset|checkout|commit|merge|rebase)"
    r"|(?P<svc>reboot|shutdown)"
    r")(?=\s|$)"
)

_READ_ONLY_HEADS = (
    "echo", "printf", "ls", "cat", "head", "tail", "grep", "egrep", "rg", "awk", "jq",
    "sed", "test", "which", "whoami", "id", "date", "uptime", "df", "du", "free",
    "ss", "netstat", "ps", "env", "printenv", "curl", "wget", "ping", "dig", "nslookup",
)


def _strip_prefixes(text: str) -> str:
    """剥掉段首的 sudo/do/nohup 之类前缀与变量赋值（可能叠多层）。"""
    out = text
    for _ in range(4):
        new = _LEAD_PREFIX_RE.sub("", out)
        new = _ASSIGN_RE.sub("", new, count=1)
        if new == out:
            break
        out = new
    return out.strip()


def _verb_from_leading(text: str) -> Optional[str]:
    m = _LEADING_WRITE_RE.match(_strip_prefixes(text))
    if not m:
        return None
    for group, value in m.groupdict().items():
        if group != "lead" and value:
            return f"{group}:{value.lower()}"
    return None


def write_verb(command: str) -> Optional[str]:
    """从命令里认出「写类动作」并归一成短标签；认不出返回 None（只读/探测）。

    只在**段首**（剥掉 sudo/赋值前缀后）认，避免 `grep -nE 'scp|ssh'` 这类文本
    把只读命令误判成写操作；`ssh host 'kubectl apply …'` 这种远程包装再对引号
    内的内容做一次段首匹配。
    """
    text = str(command or "")
    if not text.strip():
        return None
    verb = _verb_from_leading(text)
    if verb:
        return verb
    # 引号内的远程命令（`ssh host 'kubectl apply …'`）再试一次段首匹配；
    # 但 grep/awk/sed 的模式串不是命令，跳过，否则 `grep 'scp |ssh'` 会误判。
    head = _strip_prefixes(text).split(None, 1)[0].lstrip("./") if text.strip() else ""
    if head in {"grep", "egrep", "rg", "awk", "sed", "find", "ls", "cat"}:
        return None
    for quoted in _QUOTED_RE.findall(text):
        inner = quoted[1:-1].strip()
        if not inner:
            continue
        for part in _SEG_SPLIT_RE.split(inner):
            verb = _verb_from_leading(part)
            if verb:
                return verb
    return None


def looks_read_only(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return True
    if write_verb(text):
        return False
    # 含 -X POST/PUT/PATCH/DELETE 或 --data 的 curl 也算写。
    # 数据类 flag 区分大小写——`-d` 是 data，`-D` 是 dump-header（只读探测）。
    if re.search(r"(?i)\bcurl\b", text) and re.search(
        r"(?i:-X\s*(?:POST|PUT|PATCH|DELETE))|--data\b|(?<![A-Za-z])-d\s|--upload-file|(?<![A-Za-z])-F\s",
        text,
    ):
        return False
    return True


_SEG_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")


def _segments(command: str) -> list[str]:
    """按链式操作符拆段（管道不拆：`curl ... | head` 仍算一条）。"""
    return [seg.strip() for seg in _SEG_SPLIT_RE.split(str(command or "")) if seg.strip()]


def _segment_is_upload(seg: str) -> bool:
    """该段是不是「带数据体的 HTTP 请求」（curl/wget 上传）。

    只看管道的第一节：`curl … | tr -d '\\n'` 里的 `-d` 属于 tr，不算上传。
    """
    head = seg.split("|", 1)[0]
    if not re.search(r"(?i)\b(curl|wget)\b", head):
        return False
    return bool(
        re.search(
            r"(?i:-X\s*(?:POST|PUT|PATCH|DELETE))|--data\b|(?<![A-Za-z])-d\s|--upload-file|(?<![A-Za-z])-F\s",
            head,
        )
    )


def classify_terminal(command: str) -> dict | None:
    """写类终端命令 → 一条变更记录；只读/探测命令返回 None。

    按链式段逐段判定：只有「真正会改东西的那一段」算变更，detail 也只取那一段，
    避免把 `sleep 20; echo xxx` 这种复合命令整条记成写操作。
    """
    for seg in _segments(command):
        verb = write_verb(seg)
        if verb:
            return {"kind": "命令", "target": verb, "detail": clean(seg, 110), "why": ""}
        if _segment_is_upload(seg):
            return {"kind": "命令", "target": "http:upload", "detail": clean(seg, 110), "why": ""}
    return None


def purpose_of(text: str, path: str = "") -> str:
    """从写入内容里取一句「这个文件是干什么的」：优先注释行，其次首个有效行。"""
    body = str(text or "")
    if not body.strip():
        return f"空文件 {Path(path).name}" if path else "空内容"
    lines = [ln.strip() for ln in body.splitlines()]
    comment = None
    first_code = None
    for line in lines[:40]:
        if not line or line.startswith("#!"):
            continue
        if re.match(r"^(#|//|/\*|\*|--)+\s*\S", line):
            cleaned = re.sub(r"^(#|//|/\*|\*|--)+\s*", "", line).strip()
            if cleaned and not cleaned.startswith("-*-"):
                comment = cleaned
                break
        elif first_code is None:
            first_code = line
    # YAML/k8s：优先 kind/name
    if not comment:
        for line in lines[:40]:
            if re.match(r"^(kind|name|metadata|apiVersion):", line):
                first_code = first_code or line
                break
    return clean(comment or first_code or "", 80)


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------

def collect(
    db: Path,
    since: datetime,
    until: datetime,
    extra_job_ids: Optional[Sequence[str]] = None,
    *,
    job_id: Optional[str] = None,
    job_name: Optional[str] = None,
    week: Optional[str] = None,
) -> dict:
    """采集一个窗口的 facts。

    ``job_id``/``job_name`` 由内置 cron 运行器直接注入；None 时回退到
    ``VIGIL_CRON_JOB_ID``（兼容预跑脚本路径）。
    """
    del job_name  # 保留参数供内置运行器传递；排除口径只认 job id
    if not db.is_file():
        raise FileNotFoundError(f"state.db 不存在：{db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lo, hi = since.timestamp(), until.timestamp()

    # 自身排除：
    # - 有 job id：只排除本 job 的 cron 会话及其派生 subagent（别把别的定时作业
    #   干的真活抹掉）。
    # - 无 job id（手动跑）：排除全部 cron/subagent 会话。
    effective_job_id = str(
        job_id if job_id is not None else os.environ.get("VIGIL_CRON_JOB_ID") or ""
    ).strip()
    extra_excluded = {str(x).strip() for x in (extra_job_ids or []) if str(x).strip()}

    sessions = conn.execute(
        "select id, source, parent_session_id, started_at, ended_at, end_reason, message_count,"
        " tool_call_count, input_tokens, output_tokens from sessions"
        " where started_at >= ? and started_at < ? order by started_at", (lo, hi)).fetchall()

    prefixes = ([f"cron_{effective_job_id}_"] if effective_job_id else []) + [
        f"cron_{jid}_" for jid in extra_excluded
    ]
    own_ids = {
        row["id"] for row in sessions
        if prefixes and row["source"] == "cron" and any(str(row["id"]).startswith(p) for p in prefixes)
    }
    # 级联：当前 job 会话派生的 subagent 也算自身。
    changed = True
    while changed and own_ids:
        changed = False
        for row in sessions:
            if row["id"] in own_ids:
                continue
            if row["parent_session_id"] in own_ids:
                own_ids.add(row["id"])
                changed = True

    tasks, skipped = [], 0
    for row in sessions:
        if row["id"] in own_ids:
            skipped += 1
            continue
        if not effective_job_id and row["source"] in EXCLUDED_SOURCES:
            skipped += 1
            continue

        asks = []
        for m in conn.execute("select content from messages where session_id=? and role='user'"
                              " order by id", (row["id"],)):
            text = str(m["content"] or "")
            if not text.strip() or CRON_HDR.match(text):
                continue
            asks.append(ask_line(text))
            if len(asks) >= 3:
                break

        changes, seen = [], set()
        objects: List[dict] = []
        obj_seen: Dict[tuple, dict] = {}
        monitor_evidence = ""
        # 工具结果按 tool_call_id 索引：结果用来填「结果」列（state=up / SELECT 1 / 2xx）
        results: Dict[str, str] = {}
        msgs = conn.execute(
            "select id, role, content, tool_call_id, tool_calls from messages"
            " where session_id=? order by id", (row["id"],)
        ).fetchall()
        for m in msgs:
            if str(m["role"] or "") == "tool" and m["tool_call_id"]:
                results[str(m["tool_call_id"])] = str(m["content"] or "")
        for m in msgs:
            for call in _json_list(m["tool_calls"]):
                fn = (call or {}).get("function") or {}
                name, raw = fn.get("name"), fn.get("arguments")
                args = _json(raw)
                res = results.get(str((call or {}).get("id") or ""), "")
                source = f"{str(row['id'])[:12]}#{m['id']}"
                item = None
                if name == "terminal":
                    item = classify_terminal(str(args.get("command") or ""))
                else:
                    item = change_from_call(str(name or ""), raw)
                if item:
                    key = (item["kind"], item["target"])
                    if key in seen:
                        for existing in changes:
                            if (existing["kind"], existing["target"]) == key and item.get("why"):
                                existing["why"] = item["why"]
                    else:
                        seen.add(key)
                        changes.append(item)
                # 变更对象（报告主体）：与 tools/命令 解耦，认不出就跳过
                objs = objects_from_call(str(name or ""), args, res, source)
                if not objs and name == "terminal":
                    cmd = str(args.get("command") or "")
                    if re.search(r"(?i)/-/reload|prometheus", cmd):
                        monitor_evidence = monitor_evidence or _result_summary(res)
                for obj in objs:
                    okey = (obj["type"], obj["identity"], obj["params"])
                    if okey in obj_seen:
                        kept = obj_seen[okey]
                        if not kept["result"] and obj["result"]:
                            kept["result"] = obj["result"]
                        kept["hits"] = int(kept.get("hits") or 1) + 1
                    else:
                        obj["hits"] = 1
                        obj_seen[okey] = obj
                        objects.append(obj)

        objects = _coalesce_accounts(objects)
        objects = _coalesce_configs(objects)
        objects = _coalesce_topo(objects)
        objects = _coalesce_monitor(objects)
        _fill_monitor_results(objects, monitor_evidence)

        if not changes and not asks and not objects:
            continue
        intents = group_into_intents(objects, asks[0] if asks else "")
        tasks.append({
            "when": datetime.fromtimestamp(row["started_at"], SHANGHAI).strftime("%m-%d %H:%M"),
            "session": row["id"],
            "source": row["source"],
            "asks": asks,
            "changes": changes[:MAX_CHANGES_PER_TASK],
            "change_total": len(changes),
            "objects": objects[:MAX_OBJECTS_PER_TASK],
            "object_total": len(objects),
            "with_identity": sum(1 for o in objects if o["identity"]),
            "intents": intents,
            "end_reason": row["end_reason"],
        })

    by_kind: Dict[str, int] = {}
    for task in tasks:
        for ch in task["changes"]:
            by_kind[ch["kind"]] = by_kind.get(ch["kind"], 0) + 1

    by_type: Dict[str, int] = {}
    objects_total = with_identity = 0
    for task in tasks:
        objects_total += task["object_total"]
        with_identity += task["with_identity"]
        for obj in task["objects"]:
            by_type[obj["type"]] = by_type.get(obj["type"], 0) + 1
    missing_identity = objects_total - with_identity

    days: List[dict] = []
    for index, task in enumerate(tasks):
        day = task["when"][:5]
        bucket = next((d for d in days if d["date"] == day), None)
        if bucket is None:
            bucket = {"date": day, "task_indexes": [], "changes": 0, "kinds": {}}
            days.append(bucket)
        bucket["task_indexes"].append(index)
        bucket["changes"] += task["change_total"]
        for ch in task["changes"]:
            bucket["kinds"][ch["kind"]] = bucket["kinds"].get(ch["kind"], 0) + 1

    span = until - since
    return {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "label": label_for(since, until, week),
            "granularity": granularity(span),
            "span_days": round(span.total_seconds() / 86400, 2),
        },
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "db": str(db),
        "totals": {
            "tasks": len(tasks),
            "changes": sum(t["change_total"] for t in tasks),
            "by_kind": by_kind,
            "objects": objects_total,
            "objects_with_identity": with_identity,
            "objects_missing_identity": missing_identity,
            "by_type": by_type,
            "excluded_pipeline_sessions": skipped,
            "active_days": len(days),
        },
        "days": days,
        "tasks": tasks,
        "gaps": [] if tasks else ["本周期没有用户会话（只读库或窗口内无作业）"],
    }


def _json_list(raw):
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    return value if isinstance(value, list) else []


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def _granularity_of_window(w: dict) -> str:
    """facts 没带 ``granularity`` 时（老 facts / 手搓 facts）按窗口跨度反推。

    反推不出来就按老行为当周报——渲染绝不能因为 facts 少字段而失败。
    """
    try:
        span = datetime.fromisoformat(w["until"]) - datetime.fromisoformat(w["since"])
    except (KeyError, TypeError, ValueError):
        return "weekly"
    return granularity(span)


def render_md(facts: dict, prose: Optional[Dict[str, str]] = None) -> str:
    """渲染四段报告：子目标与结果 / 变更明细（按对象）/ 未闭环 / 下周期。

    ``prose`` 只能替换第 1/3/4 段的叙述；对象、标识、参数、结果永远来自 facts，
    这样「另一个人拿着报告能复核」才有意义。
    """
    prose = prose or {}
    w = facts["window"]
    t = facts["totals"]
    since = datetime.fromisoformat(w["since"]).strftime("%m-%d")
    until = (datetime.fromisoformat(w["until"]) - timedelta(seconds=1)).strftime("%m-%d")
    total_objs = int(t.get("objects") or 0)
    with_id = int(t.get("objects_with_identity") or 0)
    missing = int(t.get("objects_missing_identity") or 0)

    kind = str(w.get("granularity") or _granularity_of_window(w))
    lines = [
        f"# Vigil 作业{_GRANULARITY_TITLE.get(kind, '周报')} {w['label']}（{since} ~ {until}）",
        "",
    ]
    lines.append(f"- 作业会话 {t['tasks']} 个 / 变更对象 {total_objs} 个，其中 {with_id} 个带可复核标识")
    types = "、".join(f"{k} {v}" for k, v in sorted((t.get("by_type") or {}).items())) or "无"
    lines.append(f"- 对象构成：{types}")
    if missing:
        lines.append(f"- 标识缺口：{missing} 个（没有名字/URL/参数，见第 3 节）")
    lines.append(f"- 已排除流水线自身会话：{t['excluded_pipeline_sessions']} 个")

    lines += ["", "## 1. 子目标与结果", ""]
    summary = str(prose.get("summary") or "").strip()
    if summary:
        lines.extend(summary.splitlines())
        lines.append("")
    elif not facts["tasks"]:
        lines.append("本周期无作业记录。")
    else:
        for task in facts["tasks"]:
            ask = task["asks"][0] if task["asks"] else "（无用户指令记录）"
            lines.append(f"### {task['when'][:5]} · {ask}")
            lines.append("")
            intents = task.get("intents") or []
            if not intents:
                lines.append(f"- 该会话没有可复核的变更（只读或中断，结束状态 {task['end_reason'] or '-'}）")
            for intent in intents:
                results = "；".join(dict.fromkeys(intent["results"]))[:80]
                tail = f"（{len(intent['objects'])} 项" + (f"：{results}" if results else "") + "）"
                lines.append(f"- {intent['label']}{tail}")
            lines.append("")

    lines += ["## 2. 变更明细（按对象）", ""]
    rows = 0
    for task in facts["tasks"]:
        for intent in task.get("intents") or []:
            lines.append(f"### {intent['label']}")
            lines.append("")
            lines.append("| 对象 | 标识 / 参数 | 结果 | 溯源 |")
            lines.append("|---|---|---|---|")
            for obj in intent["objects"][:MAX_ROWS_PER_INTENT]:
                rows += 1
                ident = obj["identity"] or "标识未记录"
                cell = f"{ident}；{obj['params']}" if obj["params"] else ident
                lines.append(
                    f"| {obj['type']} | {cell} | {obj['result'] or '—'} | "
                    f"{str(obj['source']).split('#')[0]} |"
                )
            if len(intent["objects"]) > MAX_ROWS_PER_INTENT:
                lines.append(
                    f"| … | 另有 {len(intent['objects']) - MAX_ROWS_PER_INTENT} 个对象未列出 | — | — |"
                )
            lines.append("")
    if rows == 0 and not any(task.get("intents") for task in facts["tasks"]):
        lines += ["本周期没有被认定为「对象变更」的记录。", ""]

    lines += ["## 3. 未闭环与待跟进", ""]
    risks = str(prose.get("risks") or "").strip()
    pending = [
        task for task in facts["tasks"]
        if (task["end_reason"] or "") not in ("cli_close", "turn_complete")
    ]
    if risks:
        lines.extend(risks.splitlines())
        lines.append("")
    elif not pending and not facts["gaps"]:
        lines.append("无。")
    for task in pending:
        ask = task["asks"][0] if task["asks"] else task["session"]
        lines.append(f"- {task['when']} {ask}：结束状态 {task['end_reason'] or '未知'}")
    for gap in facts["gaps"]:
        lines.append(f"- 数据缺：{gap}")

    no_ident = [
        (task, obj) for task in facts["tasks"]
        for obj in task.get("objects") or [] if not obj["identity"]
    ]
    if no_ident:
        lines += ["", f"数据缺口：{len(no_ident)} 个对象没有可复核标识（只有类型，没有名字/URL/参数）："]
        for task, obj in no_ident[:10]:
            lines.append(f"- {task['when']} {obj['type']}（{task['session'][:12]}）")
        if len(no_ident) > 10:
            lines.append(f"- 另有 {len(no_ident) - 10} 个未列出")

    next_steps = str(prose.get("next") or "").strip()
    lines += ["", "## 4. 下周期", ""]
    lines.extend(next_steps.splitlines() if next_steps else ["- 待补：由撰写者根据第 3 段给出 1-3 条。"])
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 发布（配置 + credential_vault + BookStack）
# --------------------------------------------------------------------------

def _load_publish_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    publish = ((cfg.get("ops") or {}).get("digest") or {}).get("publish") or {}
    return publish if isinstance(publish, dict) else {}


def default_window() -> str:
    """``ops.digest.window``（缺省/非法回落 ``7d``）。

    只影响「命令行完全没给窗口参数」的场景：``--window`` / ``--week`` /
    ``--since`` 任意一个存在时都以显式参数为准。
    """
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return DEFAULT_WINDOW
    value = str(((cfg.get("ops") or {}).get("digest") or {}).get("window") or "").strip()
    if not value:
        return DEFAULT_WINDOW
    if not re.fullmatch(r"\d+[dhmw]", value, re.IGNORECASE) or int(value[:-1]) <= 0:
        print(f"digest: 忽略非法的 ops.digest.window={value!r}，用 {DEFAULT_WINDOW}",
              file=sys.stderr)
        return DEFAULT_WINDOW
    return value


def default_publish_target() -> str:
    """config.yaml 里显式设置的发布目标（未配置/非法返回空串）。"""
    target = str(_load_publish_config().get("target") or "").strip().lower()
    return target if target in ("none", "file", "bookstack") else ""


def publish_configured() -> bool:
    """config.yaml 里是否配置了可用的 BookStack 发布目标。"""
    publish = _load_publish_config()
    if str(publish.get("target") or "").strip().lower() != "bookstack":
        return False
    bookstack = publish.get("bookstack") or {}
    return bool(
        isinstance(bookstack, dict)
        and str(bookstack.get("base_url") or "").strip()
        and bookstack.get("book_id")
        and str(bookstack.get("credential_ref") or "").strip()
    )


def _bookstack_credentials() -> Tuple[str, int, str, str]:
    publish = _load_publish_config()
    bookstack = publish.get("bookstack") or {}
    if not isinstance(bookstack, dict) or str(publish.get("target") or "").lower() != "bookstack":
        raise DigestPublishError(
            "未配置 BookStack 发布目标：请在 config.yaml 的 "
            "ops.digest.publish.bookstack 下设置 base_url / book_id / credential_ref。"
        )
    base_url = str(bookstack.get("base_url") or "").strip()
    book_id_raw = bookstack.get("book_id")
    ref = str(bookstack.get("credential_ref") or "").strip()
    missing = [
        key for key, value in (
            ("base_url", base_url), ("book_id", book_id_raw), ("credential_ref", ref),
        ) if not value
    ]
    if missing:
        raise DigestPublishError(
            f"BookStack 发布配置缺少 {', '.join(missing)}（ops.digest.publish.bookstack）。"
        )
    try:
        book_id = int(book_id_raw)
    except (TypeError, ValueError) as exc:
        raise DigestPublishError("ops.digest.publish.bookstack.book_id 必须是整数") from exc

    from tools.credential_vault import retrieve
    try:
        raw = retrieve(ref)
    except Exception as exc:
        raise DigestPublishError(
            f"无法读取凭据 {ref!r}：{exc}；先运行 `vigil credential store {ref}` 写入"
            "（值为 token_id:token_secret）。"
        ) from exc

    raw = str(raw or "").strip()
    token_id = secret = ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise DigestPublishError(
                f"凭据 {ref!r} 不是合法 JSON；应为 token_id:token_secret 或 JSON 对象。"
            ) from exc
        token_id = str(data.get("token_id") or "")
        secret = str(data.get("token_secret") or data.get("secret") or "")
    else:
        token_id, sep, secret = raw.partition(":")
        if not sep:
            raise DigestPublishError(
                f"凭据 {ref!r} 格式不对：应为 token_id:token_secret。"
            )
    if not token_id or not secret:
        raise DigestPublishError(
            f"凭据 {ref!r} 缺少 token_id 或 token_secret；用 `vigil credential store {ref}` 更新。"
        )
    return base_url, book_id, token_id, secret


def publish_markdown(
    markdown: str,
    *,
    name: str,
    state_dir: Path,
    target: str,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """把渲染好的 Markdown 发布到 file/bookstack；返回 action/page_id/url 等。"""
    normalized = str(target or "none").strip().lower()
    if normalized == "none":
        return {"action": "none", "target": "none"}
    if normalized == "file":
        path = report_path or (state_dir / "reports" / f"{name}.md")
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return {
            "action": "updated" if existed else "created",
            "target": "file",
            "path": str(path),
        }
    if normalized == "bookstack":
        from tools.publish_targets import BookstackTarget, PublishTargetError

        base_url, book_id, token_id, secret = _bookstack_credentials()
        try:
            publisher = BookstackTarget(base_url, token_id, secret, timeout=30)
            return publisher.publish(name=name, markdown=markdown, book_id=book_id)
        except PublishTargetError as exc:
            raise DigestPublishError(str(exc)) from exc
    raise DigestPublishError(f"未知发布目标：{target!r}（支持 none/file/bookstack）")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--window", default=None,
                        help="滚动窗口，如 7d / 24h / 2w（缺省读 ops.digest.window，再缺省 7d）")
    parser.add_argument("--week", default=None, help="ISO 周，如 2026-W43")
    parser.add_argument("--since", default=None, help="窗口起点（含）")
    parser.add_argument("--until", default=None, help="窗口终点（不含）")
    parser.add_argument("--db", default=None, help="state.db（默认 <VIGIL_HOME>/state.db）")
    parser.add_argument("--state-dir", default=None, help="报告输出根目录（默认 db 所在目录）")
    parser.add_argument("--format", choices=("facts", "md"), default="facts", help="输出格式（默认 facts）")
    parser.add_argument("--out", default=None, help="写文件；缺省打印 stdout")
    parser.add_argument(
        "--prose",
        default="auto",
        help=(
            "模型撰写的叙述（JSON：{summary, risks, next}，Markdown 片段）。默认 auto ="
            "读 <state-dir>/reports/<文件名前缀><label>.prose.json（前缀按窗口粒度："
            "Vigil-作业日报- / Vigil-作业周报- / Vigil-作业报告-；旧周报前缀作兼容回落）；"
            "文件不存在则用确定性模板；传 'none' 强制模板。"
        ),
    )
    parser.add_argument(
        "--publish", choices=("none", "file", "bookstack"), default=None,
        help="发布目标（默认 none；weekly-oplog 蓝图按 ops.digest.publish 选择）",
    )
    parser.add_argument(
        "--exclude-job-id", action="append", default=None,
        help="额外排除某个 cron job 的会话（可重复，用于已删除的历史测试 job）",
    )
    parser.add_argument("--locale", default="zh", choices=("zh",), help="预留，只做 zh")


def prose_paths(state_dir: Path, label: str, prefix: str) -> List[Path]:
    """prose 候选路径：新前缀优先，再回落旧的「作业周报」前缀（D1 兼容）。

    旧 job 产出的 ``Vigil-作业周报-<label>.prose.json`` 还在，改名前后的
    label 相同时（周报窗口）必须还能读到；周报自身新旧前缀相同，只有一份。
    """
    reports = state_dir / "reports"
    paths = [reports / f"{prefix}{label}.prose.json"]
    legacy = reports / f"{_LEGACY_WEEKLY_PREFIX}{label}.prose.json"
    if legacy not in paths:
        paths.append(legacy)
    return paths


def _load_prose(state_dir: Path, label: str, spec: str, *,
                prefix: str = _LEGACY_WEEKLY_PREFIX) -> Optional[Dict[str, str]]:
    """读取模型撰写的叙述。

    ``spec``：``none`` 强制模板；``auto`` 读默认路径；其它当路径。
    文件不存在/解析失败一律回落模板（报告不能因为缺叙述而失败）。
    ``prefix`` 由窗口粒度决定（``report_prefix``）；缺省按周报，兼容旧调用方。
    """
    spec = str(spec or "auto").strip()
    if spec.lower() == "none":
        return None
    if spec.lower() == "auto":
        paths = prose_paths(state_dir, label, prefix)
        path = next((c for c in paths if c.is_file()), paths[0])
    else:
        path = Path(spec).expanduser()
    if not path.is_file():
        if spec.lower() != "auto":
            print(f"digest: prose 文件不存在，回落模板：{path}", file=sys.stderr)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"digest: prose 文件无法解析，回落模板：{path}（{exc}）", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"digest: prose 顶层必须是对象，回落模板：{path}", file=sys.stderr)
        return None
    return {k: str(data.get(k) or "") for k in ("summary", "risks", "next")}


def _run(args, *, job_id: Optional[str] = None, job_name: Optional[str] = None) -> int:
    now = datetime.now(SHANGHAI)
    if not (args.window or args.week or args.since or args.until):
        args.window = default_window()
    try:
        since, until, label = parse_window(args, now)
    except ValueError as exc:
        print(f"digest: {exc}", file=sys.stderr)
        return 2
    span = until - since
    prefix = report_prefix(span)

    home = Path(os.environ.get("VIGIL_HOME") or (Path.home() / ".vigil"))
    db = Path(args.db).expanduser() if args.db else home / "state.db"
    if not db.is_file():
        print(f"digest: state.db 不存在：{db}", file=sys.stderr)
        return 3
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else db.resolve().parent

    try:
        facts = collect(
            db, since, until,
            extra_job_ids=args.exclude_job_id,
            job_id=job_id,
            job_name=job_name,
            week=args.week,
        )
    except (sqlite3.Error, OSError) as exc:
        print(f"digest: 无法读取 state.db：{exc}", file=sys.stderr)
        return 3

    target = str(args.publish or "none").strip().lower()
    prose = _load_prose(state_dir, label, args.prose, prefix=prefix)

    if target == "none":
        if args.format == "facts":
            output = json.dumps(facts, ensure_ascii=False, indent=2)
        else:
            output = render_md(facts, prose)
        if args.out:
            path = Path(args.out).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output, encoding="utf-8")
            print(f"written: {path}")
        else:
            print(output)
        return 0

    if args.format != "md":
        print("digest: --publish 需要配合 --format md（发布的是渲染稿）", file=sys.stderr)
        return 2

    markdown = render_md(facts, prose)
    default_path = state_dir / "reports" / f"{prefix}{label}.md"
    report_path = Path(args.out).expanduser() if args.out else default_path
    name = report_path.stem
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        existed = report_path.exists()
        report_path.write_text(markdown, encoding="utf-8")
        if target == "file":
            result = {
                "action": "updated" if existed else "created",
                "target": "file",
                "path": str(report_path),
            }
        else:
            result = publish_markdown(
                markdown,
                name=name,
                state_dir=state_dir,
                target=target,
                report_path=report_path,
            )
    except DigestPublishError as exc:
        print(f"digest: 发布失败：{exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"digest: 写出报告失败：{exc}", file=sys.stderr)
        return 4

    action = str(result.get("action") or "unknown")
    page_id = result.get("page_id")
    url = result.get("url")
    fields = [f"action={action}"]
    if page_id is not None:
        fields.append(f"page_id={page_id}")
    if url:
        fields.append(f"url={url}")
    fields.append(f"md={report_path}")
    print(" ".join(fields))
    return 0


def run_from_args(args, *, job_id: Optional[str] = None,
                  job_name: Optional[str] = None) -> int:
    """供 hermes_cli 子命令直接调用（args 已是 argparse.Namespace）。"""
    return _run(args, job_id=job_id, job_name=job_name)


def build_parser(prog: str = "vigil digest") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Vigil 作业日志汇总（我做了什么 → 改了什么）",
    )
    add_arguments(parser)
    return parser


def main(argv: Optional[Sequence[str]] = None, *, job_id: Optional[str] = None,
         job_name: Optional[str] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return _run(args, job_id=job_id, job_name=job_name)


if __name__ == "__main__":
    sys.exit(main())
