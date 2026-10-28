"""Automation Blueprints — parameterized automation blueprints with typed slots.

A *blueprint* is a one-place definition of an automation that every surface
renders natively:

  * Dashboard / GUI app  -> a form (one field per slot)
  * CLI / TUI / messenger -> a pre-filled ``/blueprint`` slash command
  * Agent                 -> a seed prompt; it asks for any blank/ambiguous slot
  * Docs catalog          -> a copy-paste command + a ``vigil://`` deep-link

The single source of truth is the slot schema below. ``blueprint_form_schema``
emits what a form renderer needs; ``blueprint_slash_command`` emits the flattened
one-line command; ``fill_blueprint`` validates user-supplied values and turns a
blueprint into a ``cron.jobs.create_job`` kwargs dict (so there is no second job
engine). The form-where-there's-a-screen / agent-fills-where-there's-a-chat
split both consume this same module.

Design choice: users never type raw cron. A blueprint carries a fixed recurrence
in ``schedule_template`` and parameterizes only the human-friendly parts
(time-of-day, weekday set). Blueprints needing full flexibility expose a ``text``
slot named ``schedule`` that passes through verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "BlueprintSlot",
    "AutomationBlueprint",
    "CATALOG",
    "get_blueprint",
    "blueprint_form_schema",
    "blueprint_slash_command",
    "blueprint_deeplink",
    "blueprint_catalog_entry",
    "fill_blueprint",
    "BlueprintFillError",
    "WEEKDAY_PRESETS",
]


class BlueprintFillError(ValueError):
    """Raised when supplied slot values fail validation."""


# Slot types the renderers understand.
_SLOT_TYPES = frozenset({"time", "enum", "text", "weekdays"})

# Named weekday recurrences -> cron day-of-week field.
WEEKDAY_PRESETS: Dict[str, str] = {
    "everyday": "*",
    "weekdays": "1-5",
    "weekends": "0,6",
}


@dataclass(frozen=True)
class BlueprintSlot:
    """A single fillable field on a blueprint."""

    name: str
    type: str
    label: str
    default: Any = None
    options: tuple = ()       # for type="enum": allowed values
    optional: bool = False
    help: str = ""
    # When False, ``options`` are suggestions rather than a closed set —
    # any value is accepted (e.g. the deliver slot, where the real set of
    # valid platforms depends on the user's configured gateways and is
    # validated downstream by the cron scheduler).
    strict: bool = True

    def __post_init__(self) -> None:
        if self.type not in _SLOT_TYPES:
            raise ValueError(f"unknown slot type {self.type!r} (slot {self.name})")


@dataclass(frozen=True)
class AutomationBlueprint:
    """A parameterized automation blueprint."""

    key: str
    title: str
    description: str
    category: str
    # Cron expression with ``{slot}`` placeholders, e.g. "{minute} {hour} * * {dow}".
    # Placeholders are filled from resolved slot values (time -> minute/hour,
    # weekdays -> dow). A literal cron string with no placeholders = fixed schedule.
    schedule_template: str
    # Seed instruction for the agent / the cron job prompt; may contain {slot}s.
    prompt_template: str
    slots: List[BlueprintSlot] = field(default_factory=list)
    deliver_default: str = "origin"
    skills: tuple = ()        # skills the job loads before running
    tags: tuple = ()
    # Optional builtin script. When set, fill_blueprint emits
    # script=<name> (e.g. "builtin:digest") instead of a physical file path.
    script: str = ""


# ---------------------------------------------------------------------------
# Curated in-repo catalog
# ---------------------------------------------------------------------------

_TIME = lambda default="08:00": BlueprintSlot(  # noqa: E731 - concise factory
    name="time", type="time", label="What time?", default=default,
    help="24h local time, e.g. 08:00",
)
_DELIVER = BlueprintSlot(
    name="deliver", type="enum", label="Where to deliver?",
    default="origin", options=("origin", "local", "telegram", "discord", "email"),
    optional=False, strict=False,
    help="origin = the chat you set this up from (or your configured home "
    "channel when created from the dashboard); local = save only, no message; "
    "or any connected platform name",
)


CATALOG: List[AutomationBlueprint] = [
    AutomationBlueprint(
        key="morning-brief",
        title="Morning briefing",
        description="A short daily briefing: today's calendar, weather, and "
        "anything urgent waiting on you.",
        category="daily",
        schedule_template="{minute} {hour} * * *",
        prompt_template=(
            "Produce a concise morning briefing for the user: today's calendar "
            "events, the local weather, and any urgent items. Keep it short and "
            "scannable. If no data sources are connected, give a brief "
            "good-morning with the date and offer to connect calendar/email."
        ),
        slots=[_TIME("08:00"), _DELIVER],
        tags=("daily", "briefing"),
    ),
    AutomationBlueprint(
        key="important-mail",
        title="Important-mail monitor",
        description="Check your inbox periodically and ping you ONLY about mail "
        "that actually needs attention.",
        category="email",
        schedule_template="*/{interval_min} * * * *",
        prompt_template=(
            "Check the user's inbox for new messages since the last run. Surface "
            "ONLY mail matching: {criteria}. Score candidates with the urgency "
            "classifier and deliver only what clears the bar; if nothing does, "
            "respond with [SILENT]. Requires a connected mail source; if none is "
            "configured, explain how to connect one and stop."
        ),
        slots=[
            BlueprintSlot(
                name="interval_min", type="enum", label="How often?",
                default="30", options=("15", "30", "60"),
                help="minutes between checks",
            ),
            BlueprintSlot(
                name="criteria", type="text",
                label="Only notify me if the mail…",
                default="needs a reply today, is from my manager or family, "
                "or mentions a deadline",
            ),
            _DELIVER,
        ],
        tags=("email", "monitor"),
    ),
    AutomationBlueprint(
        key="weekly-review",
        title="Weekly review",
        description="A weekly recap: what got done, what's still open, and "
        "what's coming up.",
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Produce a weekly review for the user: what was accomplished this "
            "week, still-open items, and next week's calendar. Pull from "
            "connected sources. Keep it tight."
        ),
        slots=[
            _TIME("18:00"),
            BlueprintSlot(
                name="day", type="enum", label="Which day?",
                default="sunday",
                options=("sunday", "monday", "friday", "saturday"),
            ),
            _DELIVER,
        ],
        tags=("weekly", "review"),
    ),
    AutomationBlueprint(
        key="weekly-oplog",
        title="Weekly work log (digest)",
        description="按「我提的任务 → 实际做的变更」汇总一周，出 Markdown 并可发布到 BookStack",
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "你是 Vigil 作业周报撰写者。上面注入的 JSON 是采集器从 state.db "
            "确定性抽取的作业事实：每个 task 是「用户提出的任务」，task.changes "
            "是这次任务里真正落地的变更（拓扑字段、文件、runbook、技能、命令）。\n\n"
            "禁止修改任何数字、时间、对象名、参数值；表格必须与 JSON 逐值一致。\n\n"
            "按 facts.days 分组写第 1 段。固定五段：\n"
            "## 1. 本周做了什么\n"
            "每个 task 一行，写成「做了什么 → 结果」的人话；按天分组，不超过 8 行。\n"
            "## 2. 变更明细（服务 / 配置 / 参数）\n"
            "表格五列：时间 | 类型 | 对象 | 变更内容 | 会话。只放 kind 为 "
            "拓扑/文件/runbook/技能/发布/记忆 的行，参数值原样保留。\n"
            "## 3. 执行过的写类命令（摘）\n"
            "每行一条：时间 [动作] 命令摘要；最多 12 条，超出写「另有 N 条未列出」。\n"
            "## 4. 未闭环与待跟进\n"
            "只写 end_reason 不是 cli_close/turn_complete 的任务、以及 gaps。\n"
            "## 5. 下周期\n"
            "1-3 条，必须对应第 4 段的未闭环项；没有就写「无」。\n\n"
            "写完后把 Markdown 写到 facts.db 所在目录的 reports/ 下，文件名按 "
            "facts.window.granularity 取前缀：daily→Vigil-作业日报-，weekly→"
            "Vigil-作业周报-，range→Vigil-作业报告-，后面接 <facts.window.label>"
            "（label 形如 2026-10-28 / 2026-W44 / 2026-07-30_2026-10-28）；然后调用 "
            "publish_bookstack 发布：name=文件名去 .md，markdown=刚写文件的内容，"
            "book_id 由工具配置解析。禁止用 curl/terminal 自行发布；发布失败最多"
            "重试 1 次，仍失败只报错误，禁止编造 URL。最后回报两行：MD 绝对路径；"
            "BookStack 返回的 action + url。"
        ),
        slots=[
            _TIME("18:00"),
            BlueprintSlot(
                name="day", type="enum", label="Which day?",
                default="friday",
                options=("friday", "sunday", "monday", "saturday"),
            ),
            BlueprintSlot(
                name="window", type="enum", label="Window",
                default="7d", options=("7d", "14d", "30d"), strict=False,
                help=(
                    "digest 的时间窗：任意 <N>[dhmw]（1d / 12h / 90d 都行；"
                    "7d/14d/30d 只是建议）。≤2 天出日报、≤10 天出周报、>10 天出"
                    "区间报告。语法错误由 digest 运行期报错。也可在 config 里设 "
                    "ops.digest.window 作缺省"
                ),
            ),
            BlueprintSlot(
                name="publish", type="enum", label="Publish to",
                default="bookstack", options=("bookstack", "file", "none"),
                help="bookstack 需要先配置 ops.digest.publish",
            ),
            _DELIVER,
        ],
        script="builtin:digest",
        tags=("weekly", "digest"),
    ),
    AutomationBlueprint(
        key="workday-start",
        title="Workday start reminder",
        description="A weekday nudge with your agenda and top priorities.",
        category="daily",
        schedule_template="{minute} {hour} * * 1-5",
        prompt_template=(
            "Give the user a brief weekday start-of-day nudge: today's calendar "
            "and the 1-3 highest-priority things to focus on, inferred from "
            "recent context and any task tools. Encouraging, short, one message."
        ),
        slots=[_TIME("09:00"), _DELIVER],
        tags=("daily", "focus"),
    ),
    AutomationBlueprint(
        key="custom-reminder",
        title="Custom reminder",
        description="A recurring reminder in your own words, on your schedule.",
        category="general",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template="Remind the user: {what}",
        slots=[
            BlueprintSlot(name="what", type="text", label="Remind me to…",
                       default="take a break and stretch"),
            _TIME("14:00"),
            BlueprintSlot(
                name="recurrence", type="weekdays", label="Repeat on",
                default="everyday",
                options=tuple(WEEKDAY_PRESETS.keys()),
            ),
            _DELIVER,
        ],
        tags=("reminder",),
    ),
    AutomationBlueprint(
        key="evening-winddown",
        title="Evening wind-down",
        description="An end-of-day check-in: tomorrow's calendar at a glance "
        "and anything you should prep tonight.",
        category="daily",
        schedule_template="{minute} {hour} * * *",
        prompt_template=(
            "Give the user a short evening wind-down: tomorrow's calendar, any "
            "early commitments to prep for, and one gentle nudge to wrap up "
            "loose ends from today. Keep it calm and brief — one message. If no "
            "calendar is connected, just offer a friendly sign-off and the "
            "weather for tomorrow."
        ),
        slots=[_TIME("21:00"), _DELIVER],
        tags=("daily", "evening"),
    ),
    AutomationBlueprint(
        key="news-digest",
        title="Topic news digest",
        description="A recurring digest on a topic you care about — deduped "
        "against what was already sent, so only genuinely new items land.",
        category="general",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Search the web for new and noteworthy items about: {topic}. "
            "Dedupe against what you sent in previous runs — only include "
            "genuinely new developments. Deliver a tight digest of at most "
            "{count} bullets, each one line with a link. If nothing new since "
            "last run, respond with [SILENT]."
        ),
        slots=[
            BlueprintSlot(
                name="topic", type="text", label="What topic?",
                default="AI and technology",
                help="a subject, product, person, or search phrase",
            ),
            _TIME("18:00"),
            BlueprintSlot(
                name="recurrence", type="weekdays", label="Repeat on",
                default="weekdays",
                options=tuple(WEEKDAY_PRESETS.keys()),
            ),
            BlueprintSlot(
                name="count", type="enum", label="How many bullets?",
                default="5", options=("3", "5", "8"),
            ),
            _DELIVER,
        ],
        tags=("digest", "research"),
    ),
    AutomationBlueprint(
        key="bill-renewal-watch",
        title="Bills & renewals reminder",
        description="A heads-up before a recurring payment, subscription "
        "renewal, or due date — so nothing auto-charges by surprise.",
        category="general",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Remind the user about an upcoming payment or renewal: {what}. "
            "Phrase it as an actionable heads-up (e.g. 'review or cancel before "
            "it renews'), not just a notification. One short message."
        ),
        slots=[
            BlueprintSlot(
                name="what", type="text", label="What's due?",
                default="my streaming subscription renews soon",
            ),
            _TIME("10:00"),
            BlueprintSlot(
                name="recurrence", type="weekdays", label="Repeat on",
                default="everyday",
                options=tuple(WEEKDAY_PRESETS.keys()),
            ),
            _DELIVER,
        ],
        tags=("reminder", "finance"),
    ),
    AutomationBlueprint(
        key="habit-checkin",
        title="Habit check-in",
        description="A recurring nudge to keep a habit on track and reflect "
        "on whether you did it.",
        category="general",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Nudge the user about their habit: {habit}. Ask whether they did it "
            "today, keep it warm and non-judgmental, and offer a one-line word "
            "of encouragement. One short message."
        ),
        slots=[
            BlueprintSlot(
                name="habit", type="text", label="Which habit?",
                default="20 minutes of reading",
            ),
            _TIME("20:00"),
            BlueprintSlot(
                name="recurrence", type="weekdays", label="Repeat on",
                default="everyday",
                options=tuple(WEEKDAY_PRESETS.keys()),
            ),
            _DELIVER,
        ],
        tags=("habit", "wellbeing"),
    ),
    AutomationBlueprint(
        key="hydration-move",
        title="Hydration & movement nudge",
        description="A periodic nudge during the day to drink water, stand up, "
        "and stretch.",
        category="general",
        # NOTE: cron minute-field steps (*/90) wrap per hour — */90 and */120
        # both degrade to hourly. Use an hour-field step instead so the chosen
        # cadence is what actually fires.
        schedule_template="0 {start_hour}-{end_hour}/{interval_hours} * * 1-5",
        prompt_template=(
            "Send the user a brief, friendly nudge to drink some water, stand "
            "up, and stretch for a moment. Vary the wording each time so it "
            "doesn't feel robotic. One short line."
        ),
        slots=[
            BlueprintSlot(
                name="interval_hours", type="enum", label="How often?",
                default="1", options=("1", "2", "3"),
                help="hours between nudges",
            ),
            BlueprintSlot(
                name="start_hour", type="enum", label="Start hour",
                default="9", options=("7", "8", "9", "10"),
                help="first hour of the active window (24h)",
            ),
            BlueprintSlot(
                name="end_hour", type="enum", label="End hour",
                default="17", options=("16", "17", "18", "19"),
                help="last hour of the active window (24h)",
            ),
            _DELIVER,
        ],
        tags=("wellbeing", "focus"),
    ),
    AutomationBlueprint(
        key="meal-plan",
        title="Weekly meal plan",
        description="A weekly meal plan plus a consolidated grocery list, "
        "tuned to your diet and how much time you have to cook.",
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Build the user a meal plan for the coming week: {meals} per day, "
            "suited to a {diet} diet and roughly {effort} cooking effort. "
            "Include a consolidated grocery list grouped by aisle. Keep blueprints "
            "simple and skimmable."
        ),
        slots=[
            BlueprintSlot(
                name="diet", type="enum", label="Diet?",
                default="no restrictions",
                options=("no restrictions", "vegetarian", "vegan",
                         "high-protein", "low-carb"),
            ),
            BlueprintSlot(
                name="meals", type="enum", label="Meals per day?",
                default="dinner only",
                options=("dinner only", "lunch and dinner", "all three"),
            ),
            BlueprintSlot(
                name="effort", type="enum", label="Cooking effort?",
                default="quick", options=("quick", "medium", "ambitious"),
            ),
            _TIME("17:00"),
            BlueprintSlot(
                name="day", type="enum", label="Which day?",
                default="sunday",
                options=("sunday", "monday", "friday", "saturday"),
            ),
            _DELIVER,
        ],
        tags=("weekly", "food"),
    ),
    AutomationBlueprint(
        key="learn-daily",
        title="Daily learning drip",
        description="One bite-sized lesson a day on a topic you want to learn, "
        "building progressively over time.",
        category="daily",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Teach the user one bite-sized lesson about: {topic}. Build on "
            "earlier lessons so it progresses rather than repeating. Keep it to "
            "a couple of short paragraphs with one concrete example, and end "
            "with a single question to check understanding."
        ),
        slots=[
            BlueprintSlot(
                name="topic", type="text", label="Learn about…",
                default="Spanish vocabulary",
            ),
            _TIME("08:30"),
            BlueprintSlot(
                name="recurrence", type="weekdays", label="Repeat on",
                default="weekdays",
                options=tuple(WEEKDAY_PRESETS.keys()),
            ),
            _DELIVER,
        ],
        tags=("learning", "daily"),
    ),
    AutomationBlueprint(
        key="gratitude-journal",
        title="Gratitude & reflection prompt",
        description="A gentle evening prompt to reflect on the day and note "
        "what went well.",
        category="general",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=(
            "Send the user a short, warm reflection prompt for the end of the "
            "day — invite them to note one thing that went well, one thing they "
            "are grateful for, and one small win. If they reply, acknowledge it "
            "kindly. One message."
        ),
        slots=[
            _TIME("21:30"),
            BlueprintSlot(
                name="recurrence", type="weekdays", label="Repeat on",
                default="everyday",
                options=tuple(WEEKDAY_PRESETS.keys()),
            ),
            _DELIVER,
        ],
        tags=("wellbeing", "reflection"),
    ),
    AutomationBlueprint(
        key="on-this-day",
        title="On-this-day discovery",
        description="A daily dose of curiosity: a notable historical event, "
        "fact, or word for the day.",
        category="daily",
        schedule_template="{minute} {hour} * * *",
        prompt_template=(
            "Give the user one interesting '{flavor}' item for today — keep it "
            "short, surprising, and genuinely interesting. One or two sentences, "
            "no filler."
        ),
        slots=[
            BlueprintSlot(
                name="flavor", type="enum", label="What kind?",
                default="on this day in history",
                options=("on this day in history", "word of the day",
                         "science fact", "quote of the day"),
            ),
            _TIME("07:30"),
            _DELIVER,
        ],
        tags=("daily", "curiosity"),
    ),
]

_CATALOG_BY_KEY = {r.key: r for r in CATALOG}


def get_blueprint(key: str) -> Optional[AutomationBlueprint]:
    return _CATALOG_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def blueprint_form_schema(blueprint: AutomationBlueprint) -> Dict[str, Any]:
    """Emit the JSON a form renderer (dashboard / GUI) needs for this blueprint."""
    return {
        "key": blueprint.key,
        "title": blueprint.title,
        "description": blueprint.description,
        "category": blueprint.category,
        "tags": list(blueprint.tags),
        "fields": [
            {
                "name": s.name,
                "type": s.type,
                "label": s.label,
                "default": s.default,
                "options": list(s.options),
                "optional": s.optional,
                "strict": s.strict,
                "help": s.help,
            }
            for s in blueprint.slots
        ],
    }


def blueprint_slash_command(blueprint: AutomationBlueprint, values: Optional[Dict[str, Any]] = None) -> str:
    """Build the flattened ``/blueprint <key> slot=val …`` command string.

    Uses each slot's default when ``values`` is omitted, so the docs/dashboard
    can show a ready-to-paste command. Free-text slots are quoted.
    """
    values = values or {}
    parts = [f"/blueprint {blueprint.key}"]
    for s in blueprint.slots:
        val = values.get(s.name, s.default)
        if val is None or val == "":
            if s.optional:
                continue
            val = ""
        sval = str(val)
        if s.type == "text" or " " in sval:
            sval = '"' + sval.replace('"', '\\"') + '"'
        parts.append(f"{s.name}={sval}")
    return " ".join(parts)


def blueprint_deeplink(blueprint: AutomationBlueprint, values: Optional[Dict[str, Any]] = None) -> str:
    """Build the ``vigil://blueprint/<key>?slot=val`` deep-link URL."""
    from urllib.parse import quote, urlencode

    values = values or {}
    query = {}
    for s in blueprint.slots:
        val = values.get(s.name, s.default)
        if val not in (None, ""):
            query[s.name] = str(val)
    qs = ("?" + urlencode(query)) if query else ""
    return f"vigil://blueprint/{quote(blueprint.key)}{qs}"


def _humanize_schedule(blueprint: AutomationBlueprint) -> str:
    """A short human-readable description of when a blueprint runs (defaults)."""
    sched = blueprint.schedule_template
    if sched.startswith("*/"):
        iv = next((s for s in blueprint.slots if s.name == "interval_min"), None)
        every = (iv.default if iv else None) or sched.split("/")[1].split()[0]
        return f"every {every} minutes"
    if "{interval_hours}" in sched:
        iv = next((s for s in blueprint.slots if s.name == "interval_hours"), None)
        every = str((iv.default if iv else None) or "1")
        scope = "weekdays, " if "* * 1-5" in sched else ""
        return f"{scope}every hour" if every == "1" else f"{scope}every {every} hours"
    time_slot = next((s for s in blueprint.slots if s.type == "time"), None)
    when = time_slot.default if time_slot else None
    if "* * 1-5" in sched:
        return f"weekdays at {when}" if when else "every weekday"
    if "{dow}" in sched:
        day_slot = next((s for s in blueprint.slots if s.name in ("day", "recurrence")), None)
        scope = (day_slot.default if day_slot else "") or ""
        if scope and when:
            return f"{scope} at {when}"
        return f"at {when}" if when else "on a schedule"
    if when:
        return f"daily at {when}"
    return "on a schedule"


def blueprint_catalog_entry(blueprint: AutomationBlueprint) -> Dict[str, Any]:
    """Unified serializable shape for a blueprint — used by the docs generator
    and the dashboard API. Combines the form schema, the ready-to-paste slash
    command, the deep-link URL, and a human-readable schedule.
    """
    return {
        **blueprint_form_schema(blueprint),
        "schedule": blueprint.schedule_template,
        "scheduleHuman": _humanize_schedule(blueprint),
        "command": blueprint_slash_command(blueprint),
        "appUrl": blueprint_deeplink(blueprint),
    }


# ---------------------------------------------------------------------------
# Fill + validate + translate to a create_job spec
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAY_TO_DOW = {
    "sunday": "0", "monday": "1", "tuesday": "2", "wednesday": "3",
    "thursday": "4", "friday": "5", "saturday": "6",
}


def _resolve_schedule(blueprint: AutomationBlueprint, values: Dict[str, Any]) -> str:
    """Fill the schedule_template placeholders from resolved slot values."""
    sched = blueprint.schedule_template

    # A free-text `schedule` slot passes through verbatim (full flexibility).
    if "schedule" in values and values["schedule"]:
        return str(values["schedule"])

    repl: Dict[str, str] = {}

    # time -> minute/hour
    time_val = values.get("time")
    if "{minute}" in sched or "{hour}" in sched:
        if not time_val:
            raise BlueprintFillError("a time is required")
        m = _TIME_RE.match(str(time_val).strip())
        if not m:
            raise BlueprintFillError(f"invalid time {time_val!r} — use HH:MM (24h)")
        repl["hour"] = str(int(m.group(1)))
        repl["minute"] = str(int(m.group(2)))

    # weekday set -> dow
    if "{dow}" in sched:
        if "recurrence" in values:
            preset = str(values.get("recurrence", "everyday")).lower()
            if preset not in WEEKDAY_PRESETS:
                raise BlueprintFillError(
                    f"unknown recurrence {preset!r} — one of {', '.join(WEEKDAY_PRESETS)}"
                )
            repl["dow"] = WEEKDAY_PRESETS[preset]
        elif "day" in values:
            day = str(values.get("day", "")).lower()
            if day not in _DAY_TO_DOW:
                raise BlueprintFillError(f"unknown day {day!r}")
            repl["dow"] = _DAY_TO_DOW[day]
        else:
            repl["dow"] = "*"

    # interval (minutes) for */N schedules
    if "{interval_min}" in sched:
        iv = str(values.get("interval_min", "")).strip()
        if not iv.isdigit() or int(iv) <= 0:
            raise BlueprintFillError(f"invalid interval {iv!r} — minutes as a positive integer")
        repl["interval_min"] = iv

    # Any remaining {slot} placeholders are filled verbatim from validated
    # enum/text slot values (e.g. an hour-range window). Enum options have
    # already been checked in fill_blueprint, so these are safe to interpolate.
    for name in re.findall(r"\{(\w+)\}", sched):
        if name not in repl and name in values:
            repl[name] = str(values[name])

    try:
        return sched.format(**repl)
    except KeyError as e:  # pragma: no cover - template/slot mismatch is a dev error
        raise BlueprintFillError(f"schedule template missing value for {e}") from e


def fill_blueprint(
    blueprint: AutomationBlueprint,
    values: Dict[str, Any],
    *,
    origin: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate ``values`` and return ``cron.jobs.create_job`` kwargs.

    Missing required (non-optional) slots raise BlueprintFillError naming the
    slot, so a form can show field errors and the agent knows what to ask.
    Unknown slot names are rejected (a typo'd ``tiem=07:15`` must not silently
    create a job with the default time). Enum values are checked against their
    options. The result is passed straight to ``create_job`` — no second schema.
    """
    known = {s.name for s in blueprint.slots}
    unknown = sorted(set(values) - known)
    if unknown:
        raise BlueprintFillError(
            f"unknown slot{'s' if len(unknown) > 1 else ''}: "
            f"{', '.join(unknown)} — valid: {', '.join(s.name for s in blueprint.slots)}"
        )
    resolved: Dict[str, Any] = {}
    for s in blueprint.slots:
        raw = values.get(s.name, s.default)
        if raw in (None, ""):
            if s.optional:
                continue
            raise BlueprintFillError(f"missing required value: {s.name} ({s.label})")
        if s.type == "enum" and s.strict and s.options and str(raw) not in {str(o) for o in s.options}:
            raise BlueprintFillError(
                f"{s.name}={raw!r} not allowed — one of {', '.join(map(str, s.options))}"
            )
        resolved[s.name] = raw

    # weekly-oplog 的 publish 默认值跟随 ops.digest.publish.target；未显式
    # 配置时仍按蓝图默认 bookstack，由 _apply_builtin_script 回退 file。
    if blueprint.script and "publish" in known and "publish" not in values:
        from ops.digest import default_publish_target

        configured_target = default_publish_target()
        if configured_target in ("file", "none"):
            resolved["publish"] = configured_target

    schedule = _resolve_schedule(blueprint, resolved)

    # Render the prompt with whatever slots it references.
    try:
        prompt = blueprint.prompt_template.format(**resolved)
    except KeyError as e:
        raise BlueprintFillError(f"blueprint prompt missing value for {e}") from e

    spec: Dict[str, Any] = {
        "prompt": prompt,
        "schedule": schedule,
        "name": blueprint.title,
        "deliver": resolved.get("deliver", blueprint.deliver_default),
    }
    if blueprint.skills:
        spec["skills"] = list(blueprint.skills)
    if origin is not None:
        spec["origin"] = origin
    if blueprint.script:
        _apply_builtin_script(blueprint, resolved, spec)
    return spec


def _apply_builtin_script(
    blueprint: AutomationBlueprint,
    resolved: Dict[str, Any],
    spec: Dict[str, Any],
) -> None:
    """把 builtin 脚本蓝图落成 job spec（weekly-oplog 用）。

    默认 agent 模式：``builtin:digest`` 只产 facts，agent 写五段并调用
    ``publish_bookstack``。publish=file/none 或 BookStack 未配置时回退为
    no_agent + 确定性 Markdown（未配置发布目标时明确写在 prompt 里）。
    """
    from ops.digest import publish_configured

    choice = str(resolved.get("publish") or "bookstack").strip().lower()
    window = str(resolved.get("window") or "7d")
    spec["script"] = blueprint.script
    if choice == "bookstack" and publish_configured():
        spec["script_args"] = ["--window", window, "--format", "facts"]
        try:
            from hermes_cli.config import load_config_readonly
            from hermes_cli.tools_config import _get_platform_tools

            toolsets = set(_get_platform_tools(load_config_readonly(), "cron"))
        except Exception:
            toolsets = set()
        # publish_bookstack 已从核心工具移除，只挂在 bookstack toolset；
        # 这条 agent job 显式带上它，避免用户还得手动勾选。
        toolsets.add("bookstack")
        spec["enabled_toolsets"] = sorted(toolsets)
        return

    target = choice if choice in ("file", "none") else "file"
    spec["script_args"] = ["--window", window, "--format", "md", "--publish", target]
    spec["no_agent"] = True
    if choice == "bookstack":
        spec["prompt"] = spec["prompt"] + (
            "\n\n注意：未配置 BookStack 发布目标（ops.digest.publish），"
            "本次回退为 no_agent + file。"
        )
