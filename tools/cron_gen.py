"""YAPL P4 cron 生成工具（yapl-design.md §10.7）。

LLM 生成 cron 必须工具辅助（LLM 不知道今天周几/日期，禁止手算）——本工具把
自然语言短语 → cron 表达式（5/6 段）确定性转换（规则表，无 LLM 参与），并做
时区辅助查询（验证 IANA 时区 + 返回该时区当前墙钟）。生成结果可直接填 runbook
schedule.cron。

支持短语形态：
  - 每 N 分钟 / 每 N 小时 / 每 N 天 / 每小时
  - 每天 H 点 [M 分] / 每天 HH:MM
  - 每周[星期X] H 点 [M 分]（一/二/三/四/五/六/日/天；mon..sun 英文）
  - 每月 D 日 H 点 [M 分]
  - 每季度
数字支持中文数字（一..十二、二十、二十五）与阿拉伯数字。
未匹配 → 报错引导（列出支持形态），绝不猜测手算。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_DOW_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "日": 0, "天": 0}
_DOW_EN = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5,
           "sat": 6, "sun": 0}


def _cn_to_int(text: str) -> Optional[int]:
    """中文数字 → int（支持 一..九、十、十一..十九、二十、二十一..等）。"""
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text.startswith("十"):
        tail = _CN_DIGITS.get(text[1:], 0) if len(text) > 1 else 0
        return 10 + tail
    if text.endswith("十") and len(text) > 1:
        head = _CN_DIGITS.get(text[0], 0)
        return head * 10
    if "十" in text and len(text) == 3:
        head = _CN_DIGITS.get(text[0], 0)
        tail = _CN_DIGITS.get(text[2], 0)
        return head * 10 + tail
    return None


def _parse_time(h_text: str, m_text: Optional[str] = None,
                 period: str = "") -> tuple:
    """'9' / '09' / '九' → (9, 0)；'9:30' 由调用方拆开。时段词调整：
    凌晨/早上/上午 → 原值；中午 → 12；下午/晚上 → +12（12 点不变）。"""
    h = _cn_to_int(h_text)
    if h is None or not (0 <= h <= 23):
        raise ValueError(f"小时 {h_text!r} 无法解析（0-23）")
    m = _cn_to_int(m_text) if m_text else 0
    if m is None or not (0 <= m <= 59):
        raise ValueError(f"分钟 {m_text!r} 无法解析（0-59）")
    p = str(period or "").strip()
    if "中午" in p:
        h = 12
    elif ("下午" in p or "晚上" in p) and h < 12:
        h += 12
    return h, m


def _validate_cron(expr: str) -> Optional[str]:
    try:
        from cron.jobs import _ensure_croniter
        if not _ensure_croniter():
            return "croniter 未安装——无法校验 cron 表达式"
        from croniter import croniter
        croniter(expr)
        return None
    except Exception as exc:
        return f"cron 表达式 {expr!r} 非法：{exc}"


def cron_gen(text: str, tz: str = "") -> str:
    """自然语言 → cron 表达式（确定性转换）+ 时区辅助查询。

    Args:
        text: 调度语义短语（如 "每天9点"、"每5分钟"、"每周三凌晨2点"）或
            已经是 cron 表达式（校验回显）。
        tz: 可选 IANA 时区名（验证 + 返回该时区当前时间，LLM 执行判断用）。

    Returns:
        JSON：{cron, display, timezone?, now_in_tz?, note?}；未匹配/非法 =
        tool_error 引导。
    """
    raw = str(text or "").strip()
    if not raw:
        return tool_error("text 必填——用语义短语描述调度（如 '每天9点'、"
                          "'每5分钟'），或直接给 cron 表达式校验。LLM 禁止手算"
                          "cron（不知道今天周几/日期）。")

    # 已是 cron 表达式 → 校验回显
    fields = raw.split()
    if len(fields) in (5, 6) and all(
        re.fullmatch(r"[0-9A-Za-z*?/,@#-]+", f) for f in fields
    ):
        err = _validate_cron(raw)
        if err:
            return tool_error(err)
        return _response(raw, f"cron 表达式（已校验）", tz)

    n = raw.lower()
    n = re.sub(r"[，。；、]+", " ", n)
    try:
        # 每 N 分钟/小时/天
        m = re.search(r"每\s*([0-9一二三四五六七八九十两]+)\s*分钟", n)
        if m:
            num = _cn_to_int(m.group(1))
            if num is None or num <= 0:
                raise ValueError(f"间隔 {m.group(1)!r} 无法解析")
            return _cron(f"*/{num} * * * *", f"每 {num} 分钟", tz)
        m = re.search(r"每\s*([0-9一二三四五六七八九十两]+)\s*小时", n)
        if m:
            num = _cn_to_int(m.group(1))
            if num is None or num <= 0:
                raise ValueError(f"间隔 {m.group(1)!r} 无法解析")
            return _cron(f"0 */{num} * * *", f"每 {num} 小时", tz)
        m = re.search(r"每\s*([0-9一二三四五六七八九十两]+)\s*天", n)
        if m:
            num = _cn_to_int(m.group(1))
            if num is None or num <= 0:
                raise ValueError(f"间隔 {m.group(1)!r} 无法解析")
            return _cron(f"0 0 */{num} * *", f"每 {num} 天", tz)
        if "每小时" in n:
            return _cron("0 * * * *", "每小时", tz)
        if "每季度" in n:
            return _cron("0 0 1 */3 *", "每季度", tz)
        # 每天 HH:MM
        m = re.search(r"每天\s*([0-9一二三四五六七八九十]{1,3})[:：]\s*([0-9一二三四五六七八九十]{1,3})", n)
        if m:
            h, mi = _parse_time(m.group(1), m.group(2))
            return _cron(f"{mi} {h} * * *", f"每天 {h:02d}:{mi:02d}", tz)
        # 每天 [时段词] H 点 [M 分]
        m = re.search(r"每天\s*(凌晨|早上|上午|中午|下午|晚上)?\s*([0-9一二三四五六七八九十]{1,3})\s*点(?:\s*([0-9一二三四五六七八九十]{1,3})\s*分)?", n)
        if m:
            h, mi = _parse_time(m.group(2), m.group(3), m.group(1))
            return _cron(f"{mi} {h} * * *", f"每天 {h:02d}:{mi:02d}", tz)
        # 每周[星期X] H 点 [M 分]
        m = re.search(
            r"每(?:周|星期)\s*([一二三四五六日天]|mon|tue|wed|thu|fri|sat|sun)"
            r"\s*(凌晨|早上|上午|中午|下午|晚上)?"
            r"(?:\s*([0-9一二三四五六七八九十]{1,3})\s*点)?"
            r"(?:\s*([0-9一二三四五六七八九十]{1,3})\s*分)?", n)
        if m:
            dow_s = m.group(1).lower()
            dow = _DOW_CN.get(dow_s, _DOW_EN.get(dow_s))
            if dow is None:
                raise ValueError(f"星期 {m.group(1)!r} 无法解析")
            if m.group(3) is None:
                # 只有星期 → 默认 0 点
                h, mi = 0, 0
            else:
                h, mi = _parse_time(m.group(3), m.group(4), m.group(2))
            return _cron(f"{mi} {h} * * {dow}", f"每周{dow_s} {h:02d}:{mi:02d}", tz)
        # 每月 D 日 H 点 [M 分]
        m = re.search(
            r"每月\s*([0-9一二三四五六七八九十]{1,3})\s*日"
            r"\s*(凌晨|早上|上午|中午|下午|晚上)?"
            r"(?:\s*([0-9一二三四五六七八九十]{1,3})\s*点)?"
            r"(?:\s*([0-9一二三四五六七八九十]{1,3})\s*分)?", n)
        if m:
            d = _cn_to_int(m.group(1))
            if d is None or not (1 <= d <= 31):
                raise ValueError(f"日期 {m.group(1)!r} 无法解析（1-31）")
            h, mi = _parse_time(m.group(3) or "0", m.group(4), m.group(2))
            return _cron(f"{mi} {h} {d} * *", f"每月 {d} 日 {h:02d}:{mi:02d}", tz)
    except ValueError as exc:
        return tool_error(f"cron 短语解析失败: {exc}——支持形态：每 N 分钟/小时/天、"
                          "每小时、每天 H 点[M 分]、每周[星期X] H 点、每月 D 日 "
                          "H 点、每季度；或直接给 5/6 段 cron 表达式校验")
    return tool_error(
        f"无法从 {text!r} 确定 cron 表达式——LLM 禁止手算 cron（不知道今天周几/"
        "日期）。用语义短语：'每5分钟'、'每天9点'、'每周三凌晨2点'、'每月1日3点'、"
        "'每季度'；或直接给 5/6 段 cron 表达式（工具校验回显）。"
    )


def _cron(expr: str, display: str, tz: str) -> str:
    err = _validate_cron(expr)
    if err:
        return tool_error(err)
    return _response(expr, display, tz)


def _response(expr: str, display: str, tz: str) -> str:
    payload: Dict[str, Any] = {
        "cron": expr,
        "display": display,
        "note": "cron 由工具确定性生成——可直接填 runbook schedule.cron",
    }
    if tz.strip():
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(tz.strip())
        except Exception:
            return tool_error(f"时区 {tz!r} 不是合法 IANA 时区名（如 Asia/Shanghai / UTC）")
        import datetime as _dt
        now = _dt.datetime.now(ZoneInfo(tz.strip()))
        payload["timezone"] = tz.strip()
        payload["now_in_tz"] = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DEFAULT_SCHEMA = {
    "name": "cron_gen",
    "description": (
        "生成 cron 表达式（5/6 段）——LLM 禁止手算 cron（不知道今天周几/日期），"
        "调度语义一律经本工具确定性转换。支持：'每5分钟'、'每2小时'、'每天9点'、"
        "'每天9:30'、'每周三凌晨2点'、'每月1日3点'、'每季度'（中文数字/阿拉伯数字"
        "均可）；或直接传 5/6 段 cron 表达式校验回显。可选 tz（IANA 名）验证时区"
        "并返回该时区当前墙钟时间（执行判断用）。生成结果直接填 runbook "
        "schedule.cron（配套 timezone 必填）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "调度语义短语（如 '每天9点' / '每5分钟'），或 cron 表达式校验。",
            },
            "tz": {
                "type": "string",
                "description": "可选 IANA 时区名（Asia/Shanghai / UTC）——验证 + 当前墙钟。",
            },
        },
        "required": ["text"],
    },
}


def _handler(args: Dict[str, Any], **kwargs) -> str:
    return cron_gen(text=args.get("text", ""), tz=args.get("tz") or "")


# 顶层 registry.register（工具发现机制只认模块顶层调用——_register() 包装会被
# AST 扫描跳过，导致 CLI 运行时工具不加载；YAPL P1-3 曾踩此坑）。
from tools.runbook_tools import check_runbook_requirements
registry.register(
    name="cron_gen",
    toolset="runbook",
    schema=_DEFAULT_SCHEMA,
    handler=_handler,
    check_fn=check_runbook_requirements,
    emoji="⏰",
    max_result_size_chars=4_000,
)
