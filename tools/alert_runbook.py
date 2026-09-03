"""tools/alert_runbook.py —— 告警→runbook 处置建议匹配器（batch87，OPS-DELTA #103）。

定位：**建议闭环，不是全自动修复**——AI 做发现/定位/建议，人做批准/否决。本模块
只产出「处置建议」（matched runbook + 置信度 + 匹配依据），**绝不执行 runbook**；
执行永远走 runbook_exec.execute_runbook 既有全链（矩阵/审批门/v0.1 老路径/审计），
本模块不提供任何直通通道（任务书 f/g/l 验收）。

匹配信号（任务书定案，两路融合）：
1. ``triggers`` 字段精确命中 > fuzzy 高分：runbook 的 triggers 是作者预声明的
   触发词（字符串）或结构化字段匹配（{alertname, severity, ...} 对象）；告警的
   alertname/labels/annotations.summary/instance 文本包含任一字符串触发词、或
   结构化触发词逐字段精确命中 → high（最高置信）。
2. fuzzy 模糊评分：复用 ``runbook_tools._score_runbook``（query = alertname +
   summary 拼接）——覆盖 trigger 没写全的场景 → medium + alternatives。
   不新写匹配器（任务书：复用，避免两套评分漂移）。
3. 两者都空 → 不匹配（返回 hint：无匹配 runbook，可考虑新建）。

边界：
- 只对 runbooks/*.yaml 里能解析的 runbook 打分；单个坏文件跳过（log），不炸整批
  （验收 e）；0 runbook / 目录不存在 → 优雅降级返回不匹配 + 提示。
- 匹配结果字段透明：matched_by（trigger/fuzzy）+ matched_keyword 必须标注，让
  用户知道匹配依据（防误导）。

数据流：triage_active_alerts（拉 Alertmanager 活跃告警，复用 monitoring.
fetch_active_alerts）→ 逐条 match_alert_to_runbook → {alerts + disposition,
count, matched_count}。审计（runtime/alert_triage.jsonl，best-effort）见
record_alert_triage（OPS-DELTA #103 任务 4）。
"""

from __future__ import annotations

import logging
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# fuzzy 高分阈值：_score_runbook 的语义——100 = 全等命中、50+ = query 出现在
# name/title/summary/triggers 中（子串）。50 以下是纯 token 零散命中（噪音），
# 不进建议（「两者都空 → 不匹配」）。
_FUZZY_MIN_SCORE = 50.0
_MAX_ALTERNATIVES = 3

# 审计（任务 4，追加式 JSONL，复用 runtime/ 目录语义，轻量不建库）。
_AUDIT_DIRNAME = "runtime"
_AUDIT_FILENAME = "alert_triage.jsonl"
_AUDIT_MAX_LINES = 500
_audit_lock = threading.Lock()

# ---------------------------------------------------------------------------
# runbook 枚举（只读，容错：坏文件跳过不炸批）
# ---------------------------------------------------------------------------

def _resolve_home(home: Optional[Path]) -> Path:
    """归一 home：显式传值或 VIGIL_HOME（get_hermes_home）。"""
    from hermes_constants import get_hermes_home
    return Path(home) if home is not None else Path(get_hermes_home())


def _runbooks_dir(home: Optional[Path]) -> Path:
    """runbooks 数据目录：与 runbook_tools 同一解析（profile 感知）。"""
    from tools.runbook_tools import _runbooks_dir as _rb_dir
    return _rb_dir(_resolve_home(home))


def _load_valid_runbooks(home: Optional[Path]) -> List[Dict[str, Any]]:
    """加载全部可解析 runbook（坏 YAML/坏顶层跳过 + log，不炸批）。

    返回条目含 name（文件 stem）与完整 data（title/kind/env/summary/triggers/
    steps），供打分与摘要。
    """
    directory = _runbooks_dir(home)
    if not directory.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("."):
            continue
        from tools.runbook_tools import _load_runbook
        base = _resolve_home(home)
        try:
            data = _load_runbook(base, path.stem)
        except ValueError as exc:
            logger.warning("alert_runbook: 跳过无法解析的 runbook %s: %s", path.stem, exc)
            continue
        if data is None:
            continue
        data = dict(data)
        data["name"] = path.stem
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# 单条告警 → 匹配
# ---------------------------------------------------------------------------

def _alert_corpus(alert: Dict[str, Any]) -> str:
    """告警可匹配文本：alertname + labels 值 + annotations.summary + instance。

    触发词关键词查找落在这些字段（任务书 a/b：alertname 文本、labels/annotations
    文本命中触发词）。severity 不进语料（避免 "critical" 之类泛词误命中字符串
    触发词；结构化触发词用字段精确匹配表达 severity）。
    """
    parts = [str(alert.get("alertname") or "")]
    labels = alert.get("labels")
    if isinstance(labels, dict):
        parts.extend(str(v) for v in labels.values())
    parts.append(str(alert.get("summary") or ""))
    parts.append(str(alert.get("instance") or ""))
    return " ".join(parts).lower()


def _structured_trigger_matches(t: Dict[str, Any], alert: Dict[str, Any]) -> bool:
    """结构化触发词（{alertname: X, severity: Y, ...}）逐字段精确匹配。

    识别字段：alertname/severity/instance 直读告警；其余键查 labels（不含
    alertname）与 annotations。全部字段命中才算匹配（AND 语义）。
    """
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    for key, want in t.items():
        if key == "alertname":
            actual = alert.get("alertname")
        elif key == "severity":
            actual = alert.get("severity")
        elif key == "instance":
            actual = alert.get("instance")
        else:
            actual = labels.get(key, annotations.get(key))
        if str(actual or "").strip().lower() != str(want).strip().lower():
            return False
    return True


def _runbook_trigger_hit(rb: Dict[str, Any], corpus: str,
                         alert: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """runbook 触发词命中 → (keyword 展示串, 形态, 命中文本)。

    字符串触发词：关键词出现在告警语料中（子串，casefold）。结构化触发词：逐字段
    精确匹配。多条命中取最具体：结构化（作者精确声明）> 字符串；同类取关键词最长
    （最少歧义）。
    """
    candidates: List[Tuple[str, str, str]] = []  # (排序键, keyword, 形态)
    for t in rb.get("triggers") or []:
        if isinstance(t, str):
            kw = t.strip()
            if kw and kw.lower() in corpus:
                candidates.append((f"str:{len(kw):04d}", kw, "trigger"))
        elif isinstance(t, dict) and t:
            if _structured_trigger_matches(t, alert):
                kw = ",".join(f"{k}={v}" for k, v in sorted(t.items()))
                candidates.append((f"obj:{len(t):04d}", kw, "structured"))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0]


def _fuzzy_query(alert: Dict[str, Any]) -> str:
    q = " ".join(str(x) for x in (alert.get("alertname"), alert.get("summary")) if x)
    return q.strip()


def _runbook_id(rb: Dict[str, Any]) -> str:
    return str(rb.get("name") or "")


def _matched_fields(rb: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """matched 结果的展示字段：runbook_id + 摘要（title/kind/env/steps 数）。"""
    steps = rb.get("steps")
    return {
        "runbook": _runbook_id(rb),
        "title": rb.get("title"),
        "kind": rb.get("kind"),
        "env": rb.get("env"),
        "step_count": len(steps) if isinstance(steps, list) else 0,
        **extra,
    }


def match_alert_to_runbook(alert: Dict[str, Any],
                           home: Optional[Path] = None) -> Dict[str, Any]:
    """单条告警 → 处置建议（只读匹配，绝不执行）。

    返回：
      matched=true → {matched, runbook, title, kind, env, step_count,
                      confidence: high|medium, matched_by: trigger|fuzzy,
                      matched_keyword, reason, alternatives: [≤3 次优]}
      matched=false → {matched: false, hint, alternatives: []}

    0 runbook / runbook 目录不存在 / 全部坏文件 → 优雅降级（matched=false +
    对应提示，不抛异常）。
    """
    corpus = _alert_corpus(alert)
    runbooks = _load_valid_runbooks(home)
    if not runbooks:
        return {
            "matched": False,
            "hint": "无可用 runbook（runbooks/ 为空或全部无法解析）；可考虑新建（runbook_create）。",
            "alternatives": [],
        }

    # 信号 1：triggers 精确命中（最高置信）。
    trigger_hits: List[Tuple[Tuple[str, str, str], Dict[str, Any]]] = []
    for rb in runbooks:
        hit = _runbook_trigger_hit(rb, corpus, alert)
        if hit is not None:
            trigger_hits.append((hit, rb))
    if trigger_hits:
        best_hit, best = max(trigger_hits, key=lambda t: (t[0][0], t[1].get("name", "")))
        _rank, keyword, kind = best_hit
        if kind == "structured":
            reason = f"告警字段精确命中 runbook 结构化触发词「{keyword}」"
        else:
            reason = f"告警文本命中 runbook 触发词「{keyword}」"
        return {
            "matched": True,
            **_matched_fields(best),
            "confidence": "high",
            "matched_by": "trigger",
            "matched_keyword": keyword,
            "reason": reason,
            "alternatives": _fuzzy_alternatives(runbooks, alert, exclude=_runbook_id(best)),
        }

    # 信号 2：fuzzy 模糊评分（复用 _score_runbook，覆盖 trigger 没写全的场景）。
    query = _fuzzy_query(alert)
    if query:
        from tools.runbook_tools import _score_runbook
        scored = sorted(
            ((_score_runbook(rb, query), rb) for rb in runbooks),
            key=lambda t: t[0],
            reverse=True,
        )
        best_score, best = scored[0]
        if best_score >= _FUZZY_MIN_SCORE:
            return {
                "matched": True,
                **_matched_fields(best),
                "confidence": "medium",
                "matched_by": "fuzzy",
                "matched_keyword": None,
                "reason": (
                    f"无触发词直接命中；alertname/summary 与 runbook 文本模糊匹配"
                    f"（评分 {best_score:.0f}）"
                ),
                "alternatives": _fuzzy_alternatives(runbooks, alert, exclude=_runbook_id(best)),
            }

    return {
        "matched": False,
        "hint": "无匹配 runbook；可考虑新建（runbook_create）。",
        "alternatives": [],
    }


def _fuzzy_alternatives(runbooks: List[Dict[str, Any]], alert: Dict[str, Any],
                        exclude: Optional[str]) -> List[Dict[str, Any]]:
    """次优建议：排除主命中后按 fuzzy 评分取前 ≤3（score>0 才够格当备选）。"""
    query = _fuzzy_query(alert)
    if not query:
        return []
    from tools.runbook_tools import _score_runbook
    scored = [
        (_score_runbook(rb, query), rb)
        for rb in runbooks
        if rb.get("name") != exclude
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {"name": rb.get("name"), "title": rb.get("title")}
        for score, rb in scored[:_MAX_ALTERNATIVES]
        if score > 0
    ]


# ---------------------------------------------------------------------------
# triage：活跃告警全景 + 逐条建议
# ---------------------------------------------------------------------------

def triage_alerts(alerts: List[Dict[str, Any]],
                  home: Optional[Path] = None,
                  *,
                  record: bool = True) -> Dict[str, Any]:
    """对一批活跃告警逐条匹配，返回全景 + 建议（含无匹配告警的提示）。

    record=True（默认）：每次调用落一条审计（runtime/alert_triage.jsonl，
    best-effort 失败不阻断——dogfood 回看"哪些告警匹配过/匹配方式"用，见
    record_alert_triage；任务 4 m/n 验收）。
    """
    rows: List[Dict[str, Any]] = []
    matched_count = 0
    for alert in alerts:
        disposition = match_alert_to_runbook(alert, home=home)
        if disposition.get("matched"):
            matched_count += 1
        rows.append({**alert, "disposition": disposition})
    result = {
        "alerts": rows,
        "count": len(rows),
        "matched_count": matched_count,
        "unmatched_count": len(rows) - matched_count,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if record:
        record_alert_triage(home, _audit_entry(result))
    return result


def triage_active_alerts(home: Optional[Path] = None) -> Dict[str, Any]:
    """拉 Alertmanager 活跃告警（复用 monitoring.fetch_active_alerts）→ triage。

    未配置 Alertmanager / 上游失败 → 抛 hermes_cli.monitoring 的
    MonitoringUnavailable / MonitoringUpstreamError（调用方按 503/502/tool_error
    分层处理）。
    """
    from hermes_cli.monitoring import fetch_active_alerts

    payload = fetch_active_alerts()
    return triage_alerts(payload.get("alerts") or [], home=home)


# ---------------------------------------------------------------------------
# 审计（任务 4，OPS-DELTA #103）：runtime/alert_triage.jsonl，追加式轻量
# ---------------------------------------------------------------------------

def _audit_entry(result: Dict[str, Any]) -> Dict[str, Any]:
    """一条 triage 调用的审计内容：时间/告警数/匹配数 + 逐条（alertname、命中
    runbook、匹配方式）——调触发词与匹配阈值的数据来源，不是合规台账。"""
    return {
        "ts": result.get("at"),
        "type": "alert_triage",
        "alert_count": result.get("count", 0),
        "matched_count": result.get("matched_count", 0),
        "entries": [
            {
                "alertname": (a.get("alertname") or ""),
                "severity": (a.get("severity") or ""),
                "instance": (a.get("instance") or ""),
                "matched": bool((a.get("disposition") or {}).get("matched")),
                "runbook": (a.get("disposition") or {}).get("runbook"),
                "confidence": (a.get("disposition") or {}).get("confidence"),
                "matched_by": (a.get("disposition") or {}).get("matched_by"),
                "matched_keyword": (a.get("disposition") or {}).get("matched_keyword"),
            }
            for a in result.get("alerts") or []
        ],
    }


def alert_triage_audit_path(home: Optional[Path] = None) -> Path:
    """triage 审计文件路径（稳定：<home>/runtime/alert_triage.jsonl）。"""
    from hermes_constants import get_hermes_home
    base = Path(home) if home is not None else Path(get_hermes_home())
    return base / _AUDIT_DIRNAME / _AUDIT_FILENAME


def record_alert_triage(home: Optional[Path], record: Dict[str, Any]) -> None:
    """追加一条 triage 审计（JSONL）。best-effort：失败只记日志，不阻断调用。"""
    path = alert_triage_audit_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _audit_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > _AUDIT_MAX_LINES:
                path.write_text("\n".join(lines[-_AUDIT_MAX_LINES:]) + "\n",
                                encoding="utf-8")
    except Exception as exc:
        logger.warning("alert triage 审计写入失败: %s", exc)


def recent_alert_triage(home: Optional[Path] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
    """最近 N 条 triage 审计（新→旧），供 `vigil alerts history` 回看。"""
    path = alert_triage_audit_path(home)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("alert triage 审计读取失败: %s", exc)
        return []
    rows.reverse()
    return rows[: max(1, min(int(limit) if limit else 50, 200))]
