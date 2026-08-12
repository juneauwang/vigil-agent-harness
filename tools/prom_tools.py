"""Prometheus monitoring tools for the Ops Agent Harness (OPS-DELTA #10).

Prometheus 探查是运维 harness 的基础内置能力，不该让 LLM 自己拼 curl + PromQL：

  prom_query    — PromQL 查询（只读），支持即时查询与 range 查询；
  alert_query   — 查 Alertmanager /api/v2/alerts，返回活跃告警摘要。

Endpoint/凭据全部从 config.yaml 的 ``ops.prometheus`` 块读取（endpoint /
alertmanager / vault_path），不硬编码。``ops.prometheus.endpoint`` 为空（或未
配置）→ check_fn 返回 False，两个工具都不出现在模型 schema（零 footprint，
硬约束 3）。

只读 + 防幻觉：查询失败返回 Prometheus 的原始错误（最多截断 500 字符），
不编造指标值；网络调用硬超时 10s，防 Prometheus 无响应挂死会话。

凭据注入（不落明文）：``vault_path`` 可选，指向本机保险箱
（tools/credential_vault）里的一个 JSON 凭据条目：:

    {"user": "prom_user", "pass": "s3cr3t"}

请求时解析为 HTTP Basic Auth header 注入，工具输出/日志不出现明文（LLM 不见
凭据）。读取即登记 vault 来源（供 sudo 守卫判定，见 credential_vault）。

PromQL 预校验：拒绝 shell 元字符（; | & ` $() ${} 换行等）+ 必须含合法表达式
形态（指标名/函数/字符串字面量），防 LLM 把 shell 命令当 PromQL 提交。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# 网络硬超时（秒）：Prometheus/Alertmanager 无响应时快速失败，不挂死会话。
_HTTP_TIMEOUT = 10.0
# 紧凑摘要的截断上限：最多展示的 series 数 / 每个 series 的时间点数。
_MAX_SERIES = 20
_MAX_POINTS = 30
# 原始错误文本截断长度（防超大响应撑爆上下文）。
_MAX_ERROR_CHARS = 500

# PromQL 合法字符集（宽松白名单）。不含 ; | & ` 与换行/控制字符——这些是
# shell 命令链/命令替换的元字符，PromQL 中永不出现。
_PROMQL_CHARSET_RE = re.compile(r"^[A-Za-z0-9_:.+\-*/%^=~!<>(){}[\]$\"' ,]*$")
# 显式拒绝的 shell 形态：命令链（; | &）、命令替换（$() ${}）、反引号。
_SHELL_META_RE = re.compile(r"[;|&`]|\$\{|\$\(")
# 合法表达式形态：指标名/函数/标签名 token，或引号字符串字面量。
_EXPR_TOKEN_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*|['\"][^'\"]*['\"]")
# Prometheus 时间参数合法字符（数字 + 单位）。
_DURATION_RE = re.compile(r"^[0-9]+(ms|s|m|h|d|w|y)?$")

# 常见 shell 命令前缀（带尾随空格，避免误伤同名指标如 docker_containers）：
# 以这些词开头的多词串几乎必然是 shell 命令而非 PromQL。
_SHELL_COMMAND_PREFIXES = (
    "curl ", "wget ", "kubectl ", "helm ", "docker ", "docker-compose ",
    "ansible-playbook ", "ansible ", "ssh ", "scp ", "python ", "python3 ",
    "bash ", "sh ", "zsh ", "echo ", "cat ", "grep ", "awk ", "sed ",
    "ls ", "cd ", "pwd ", "rm ", "cp ", "mv ", "top ", "free ", "df ",
    "ps ", "systemctl ", "nc ", "nmap ", "promtool ", "k3s ",
)


def _prom_config() -> Dict[str, Any]:
    """Read ``ops.prometheus`` config block (read-only)."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        return (cfg.get("ops", {}) or {}).get("prometheus", {}) or {}
    except Exception:
        return {}


def _validate_promql(query: str) -> Optional[str]:
    """PromQL 语法预校验。合法返回 None，非法返回错误文案。

    防 LLM 把 shell 命令当 PromQL 提交：先白名单字符集，再显式拒 shell
    元字符，最后要求至少一个合法表达式形态（指标名/函数/字符串字面量）。
    不是完整语法解析——语义错误由 Prometheus 服务器以原始错误返回。
    """
    if not query or not query.strip():
        return "PromQL 查询为空。"
    if not _PROMQL_CHARSET_RE.fullmatch(query):
        return (
            "PromQL 含非法字符（只允许指标名/运算符/标签/字符串字符集，"
            "不支持 shell 语法）。"
        )
    if _SHELL_META_RE.search(query):
        return (
            "PromQL 含 shell 命令链/命令替换元字符（; | & ` $() ${}），"
            "Prometheus 不接受 shell 语法，请只提交 PromQL 表达式。"
        )
    if not _EXPR_TOKEN_RE.search(query):
        return "PromQL 缺少合法表达式形态（指标名/函数/字符串字面量）。"
    stripped = query.strip()
    if stripped.lower().startswith(_SHELL_COMMAND_PREFIXES):
        return (
            "PromQL 以 shell 命令开头（如 curl/kubectl/docker），"
            "Prometheus 不接受 shell 命令，请只提交 PromQL 表达式。"
        )
    return None


def _resolve_basic_auth(cfg: Dict[str, Any]) -> Optional[str]:
    """解析 vault 注入的 HTTP Basic Auth header 值；未配置返回 None。

    ``ops.prometheus.vault_path`` 指向本机保险箱里的一个 JSON 凭据条目
    （user/pass 字段）。解析失败抛 ValueError（错误消息不含明文凭据）。
    """
    vault_path = (cfg.get("vault_path") or "").strip()
    if not vault_path:
        return None
    from tools.credential_vault import retrieve
    raw = retrieve(vault_path)
    try:
        data = json.loads(raw)
    except Exception:
        raise ValueError(
            f"vault_path {vault_path!r} 的凭据不是合法 JSON（应为 user/pass 字段对象）"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"vault_path {vault_path!r} 的凭据应为 JSON 对象（user/pass 字段）")
    user = data.get("user")
    password = data.get("pass") or data.get("password")
    if not user or not password:
        raise ValueError(f"vault_path {vault_path!r} 的凭据缺少 user/pass 字段")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _http_get(url: str, params: Dict[str, Any], auth: Optional[str]) -> httpx.Response:
    """GET 请求（硬超时）。auth 为预编码 Basic header 值（不落日志）。"""
    headers = {"Authorization": auth} if auth else {}
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        return client.get(url, params=params, headers=headers)


def _endpoint_url(cfg: Dict[str, Any]) -> str:
    return (cfg.get("endpoint") or "").strip().rstrip("/")


def _fmt_metric(metric: Dict[str, Any]) -> str:
    """series 的紧凑标签形态：name{labels}。"""
    name = metric.get("__name__") or ""
    labels = {k: v for k, v in metric.items() if k != "__name__"}
    if labels:
        body = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{body}}}" if name else f"{{{body}}}"
    return name or "?"


def _summarize_result(result: List[Dict[str, Any]], is_range: bool) -> List[str]:
    """把 Prometheus data.result 解析为紧凑结构化行（series → 时间点摘要）。"""
    lines: List[str] = []
    for i, series in enumerate(result[: _MAX_SERIES]):
        metric = series.get("metric") or {}
        head = _fmt_metric(metric)
        if is_range:
            values = series.get("values") or []
            if not values:
                lines.append(f"- {head}: 无数据点")
                continue
            if len(values) > _MAX_POINTS:
                values = values[: _MAX_POINTS]
            nums = []
            for _ts, v in values:
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    nums.append(float("nan"))
            valid = [n for n in nums if n == n]  # drop NaN
            if valid:
                lo, hi = min(valid), max(valid)
                last = nums[-1]
                lo_s = f"{lo:g}" if lo == lo else "n/a"
                hi_s = f"{hi:g}" if hi == hi else "n/a"
                last_s = f"{last:g}" if last == last else "n/a"
                extra = "" if len(values) >= len(series.get("values") or []) else f"（截断，共 {len(series.get('values') or [])} 点）"
                lines.append(f"- {head}: {len(values)} 点 · 最新 {last_s} · min {lo_s} · max {hi_s}{extra}")
            else:
                lines.append(f"- {head}: 无有效数值点")
        else:
            value = series.get("value")
            if value and len(value) >= 2:
                lines.append(f"- {head} = {value[1]}")
            else:
                lines.append(f"- {head}: 无值")
    if len(result) > _MAX_SERIES:
        lines.append(f"… 其余 {len(result) - _MAX_SERIES} 个 series 省略")
    return lines


def prom_query(
    query: str,
    step: Optional[str] = None,
    duration: Optional[str] = None,
) -> str:
    """PromQL 查询（只读）。省略 step/duration → 即时查询；两者都给出 → range 查询。"""
    cfg = _prom_config()
    endpoint = _endpoint_url(cfg)
    if not endpoint:
        return tool_error("Prometheus 未配置（ops.prometheus.endpoint 为空），prom_query 不可用。")

    if not query or not query.strip():
        return tool_error("prom_query 需要 query 参数（PromQL 表达式）。")
    invalid = _validate_promql(query)
    if invalid:
        return tool_error(f"PromQL 预校验失败：{invalid}")

    auth: Optional[str] = None
    try:
        auth = _resolve_basic_auth(cfg)
    except ValueError as exc:
        return tool_error(str(exc))

    is_range = bool(step) or bool(duration)
    if is_range:
        if not (step and duration):
            return tool_error("range 查询需要同时提供 step 与 duration（如 step='1m', duration='1h'）。")
        if not _DURATION_RE.fullmatch(step) or not _DURATION_RE.fullmatch(duration):
            return tool_error("step/duration 格式非法（应为 30s/1m/1h/24h 等时长）。")
        try:
            end = datetime.now(timezone.utc)
            start = end - _parse_duration(duration)
            params = {
                "query": query,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "step": step,
            }
        except ValueError as exc:
            return tool_error(str(exc))
        url = f"{endpoint}/api/v1/query_range"
    else:
        params = {"query": query}
        url = f"{endpoint}/api/v1/query"

    try:
        resp = _http_get(url, params, auth)
    except httpx.TimeoutException as exc:
        return tool_error(f"Prometheus 请求超时（{_HTTP_TIMEOUT}s）: {exc}")
    except httpx.HTTPError as exc:
        return tool_error(f"Prometheus 请求失败: {exc}")

    if resp.status_code != 200:
        body = resp.text[: _MAX_ERROR_CHARS]
        return tool_error(f"Prometheus 查询失败 HTTP {resp.status_code}: {body}")

    try:
        payload = resp.json()
    except ValueError:
        return tool_error(f"Prometheus 返回非 JSON: {resp.text[: _MAX_ERROR_CHARS]}")

    status = payload.get("status")
    if status == "error":
        return tool_error(f"Prometheus 查询错误: {payload.get('error', 'unknown')}")
    if status != "success":
        return tool_error(f"Prometheus 响应异常 status={status!r}")

    data = payload.get("data") or {}
    result = data.get("result") or []
    if not isinstance(result, list):
        return tool_error("Prometheus 响应缺少 result 列表。")

    kind = "range" if is_range else "instant"
    lines = _summarize_result(result, is_range=is_range)
    if not lines:
        return f"Prometheus {kind} 查询无结果（{query!r}）。"
    head = f"Prometheus {kind} 查询 {query!r}: {len(result)} 个 series"
    return head + "\n" + "\n".join(lines)


def _parse_duration(duration: str) -> timedelta:
    """把 Prometheus 时长（如 1h/30m/90s）解析为 timedelta。"""
    m = re.fullmatch(r"([0-9]+)(ms|s|m|h|d|w|y)?", duration)
    if not m:
        raise ValueError(f"duration 格式非法: {duration!r}")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    seconds = {
        "ms": n / 1000.0,
        "s": n,
        "m": n * 60,
        "h": n * 3600,
        "d": n * 86400,
        "w": n * 604800,
        "y": n * 31536000,
    }[unit]
    return timedelta(seconds=seconds)


def alert_query() -> str:
    """查 Alertmanager /api/v2/alerts，返回活跃告警摘要。"""
    cfg = _prom_config()
    alertmanager = (cfg.get("alertmanager") or "").strip().rstrip("/")
    if not alertmanager:
        return tool_error("Alertmanager 未配置（ops.prometheus.alertmanager 为空），alert_query 不可用。")

    auth: Optional[str] = None
    try:
        auth = _resolve_basic_auth(cfg)
    except ValueError as exc:
        return tool_error(str(exc))

    url = f"{alertmanager}/api/v2/alerts"
    try:
        resp = _http_get(url, {}, auth)
    except httpx.TimeoutException as exc:
        return tool_error(f"Alertmanager 请求超时（{_HTTP_TIMEOUT}s）: {exc}")
    except httpx.HTTPError as exc:
        return tool_error(f"Alertmanager 请求失败: {exc}")

    if resp.status_code != 200:
        body = resp.text[: _MAX_ERROR_CHARS]
        return tool_error(f"Alertmanager 查询失败 HTTP {resp.status_code}: {body}")

    try:
        alerts = resp.json()
    except ValueError:
        return tool_error(f"Alertmanager 返回非 JSON: {resp.text[: _MAX_ERROR_CHARS]}")
    if not isinstance(alerts, list):
        return tool_error("Alertmanager 响应不是告警列表。")

    active = []
    for alert in alerts[: _MAX_SERIES]:
        labels = alert.get("labels") or {}
        status = alert.get("status") or {}
        state = status.get("state") or "active"
        name = labels.get("alertname") or "?"
        severity = labels.get("severity") or "-"
        instance = labels.get("instance") or "-"
        started = alert.get("startsAt") or "-"
        extra = ", ".join(
            f"{k}={v}" for k, v in sorted(labels.items())
            if k not in ("alertname", "severity", "instance")
        )
        line = f"- [{state}] {name} · severity={severity} · instance={instance} · 起于 {started}"
        if extra:
            line += f"\n  labels: {extra}"
        active.append(line)

    if not active:
        return "无活跃告警。"
    total = len(alerts)
    head = f"活跃告警: {total}"
    if total > _MAX_SERIES:
        head += f"（显示前 {_MAX_SERIES} 条）"
    return head + "\n" + "\n".join(active)


def check_prom_requirements() -> bool:
    """Prom tools 可用性门控：``ops.prometheus.endpoint`` 配置存在才可用。

    未配置（或 endpoint 为空）→ False，工具不出现（零 footprint，硬约束 3）。
    """
    cfg = _prom_config()
    return bool((cfg.get("endpoint") or "").strip())


# ---------------------------------------------------------------------------
# Schemas + registry
# ---------------------------------------------------------------------------

_DEFAULT_QUERY_SCHEMA = {
    "name": "prom_query",
    "description": (
        "Prometheus PromQL 查询（只读，ops.prometheus.endpoint 配置后可用）。"
        "查询平台指标（节点/容器/服务/告警规则相关），支持即时查询与 range 查询"
        "（range 需同时给 step + duration，如 step='1m', duration='1h'）。"
        "结果返回紧凑结构化摘要（series → 时间点统计），查询失败返回 Prometheus "
        "原始错误，不编造指标值。指标解释/告警关联请基于返回数据做语义层分析，"
        "不要自行拼接 curl。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "PromQL 表达式，如 up、rate(http_requests_total[5m])、sum by (job) (rate(...))。",
            },
            "step": {
                "type": "string",
                "description": "range 查询步长（如 30s/1m/5m），与 duration 同时给出时走 /api/v1/query_range。",
            },
            "duration": {
                "type": "string",
                "description": "range 查询回溯窗口（如 1h/24h），与 step 同时给出时走 /api/v1/query_range。",
            },
        },
        "required": ["query"],
    },
}

_DEFAULT_ALERT_SCHEMA = {
    "name": "alert_query",
    "description": (
        "查 Alertmanager 活跃告警（只读，ops.prometheus.alertmanager 配置后可用）。"
        "返回活跃告警摘要列表（alertname/severity/instance/labels/状态/起时）；"
        "无告警时明确返回「无活跃告警」。"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def _query_handler(args: Dict[str, Any], **kwargs) -> str:
    return prom_query(
        query=args.get("query", ""),
        step=args.get("step"),
        duration=args.get("duration"),
    )


def _alert_handler(args: Dict[str, Any], **kwargs) -> str:
    return alert_query()


registry.register(
    name="prom_query",
    toolset="prom",
    schema=_DEFAULT_QUERY_SCHEMA,
    handler=_query_handler,
    check_fn=check_prom_requirements,
    emoji="📊",
    max_result_size_chars=30_000,
)

registry.register(
    name="alert_query",
    toolset="prom",
    schema=_DEFAULT_ALERT_SCHEMA,
    handler=_alert_handler,
    check_fn=check_prom_requirements,
    emoji="🚨",
    max_result_size_chars=30_000,
)
