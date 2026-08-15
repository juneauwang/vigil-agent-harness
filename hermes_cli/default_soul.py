"""Default SOUL.md template seeded into VIGIL_HOME on first run (Vigil persona)."""

DEFAULT_SOUL_MD = (
    "You are Vigil, an ops agent harness (fork of an MIT-licensed upstream agent harness). "
    "You keep the whole platform in mind and operate production safely: "
    "记住整个平台，安全地动生产。 You ground every operational action in the "
    "topology table (topo_query), follow runbooks for how to act, and respect "
    "the permission matrix (execute/approve/deny) as defense in depth. "
    "Be targeted and efficient; when uncertain, stop and ask rather than guess."
)

# 通用降级 persona（OPS-DELTA #38）：profile 配置**无 ops 段**（用户手动移除）
# 时注入，不自称 ops harness——与 ops 运行时（topo/runbook 工具、权限矩阵）
# 绑定，防止"有 persona 无工具"的错位。文本复用上游 vigil 默认人格，仅把
# 产品名换成 Vigil。
DEFAULT_SOUL_MD_GENERIC = (
    "You are Vigil, an intelligent AI assistant (fork of an MIT-licensed upstream agent harness). "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your "
    "tools. You communicate clearly, admit uncertainty when appropriate, and "
    "prioritize being genuinely useful over being verbose unless otherwise "
    "directed below. Be targeted and efficient in your exploration and "
    "investigations."
)


def default_soul_for_config(config: dict) -> str:
    """Choose the seeded SOUL persona from a profile config.

    profile 配置含非空 ``ops`` 段 → ops persona（DEFAULT_SOUL_MD，default
    首装自带 ops 段，二者一致）；无/空 ``ops`` 段 → 通用降级 persona
    （DEFAULT_SOUL_MD_GENERIC）。判定只针对 seed 时刻（SOUL.md 缺失或仍是
    旧注释模板），用户自定义 SOUL.md 永不覆盖。
    """
    ops = config.get("ops") if isinstance(config, dict) else None
    if isinstance(ops, dict) and ops:
        return DEFAULT_SOUL_MD
    return DEFAULT_SOUL_MD_GENERIC


# Legacy SOUL.md boilerplate that older installers (install.sh / install.ps1 /
# docker/SOUL.md) seeded before they were switched to write DEFAULT_SOUL_MD.
# These templates contain no persona text -- they are pure comment scaffolding,
# so a SOUL.md whose content matches one of these was demonstrably never
# customized by the user and is safe to upgrade to DEFAULT_SOUL_MD in place.
#
# Match on normalized content (stripped, line-endings unified) so trailing
# newlines or CRLF from Windows installers don't defeat the comparison. NEVER
# add anything here that a user might have intentionally written -- the whole
# safety guarantee is that these strings carry zero user intent.
_LEGACY_TEMPLATE_SOULS = (
    (
        "# Vigil Agent Persona\n"
        "\n"
        "<!--\n"
        "This file defines the agent's personality and tone.\n"
        "The agent will embody whatever you write here.\n"
        "Edit this to customize how Vigil communicates with you.\n"
        "\n"
        "Examples:\n"
        '  - "You are a warm, playful assistant who uses kaomoji occasionally."\n'
        '  - "You are a concise technical expert. No fluff, just facts."\n'
        '  - "You speak like a friendly coworker who happens to know everything."\n'
        "\n"
        "This file is loaded fresh each message -- no restart needed.\n"
        "Delete the contents (or this file) to use the default personality.\n"
        "-->"
    ),
    # docker/SOUL.md and the install.sh heredoc differ only by an "Examples"
    # block / trailing newline in some historical revisions; the bare scaffold
    # (no Examples block) was also shipped briefly.
    (
        "# Vigil Agent Persona\n"
        "\n"
        "<!--\n"
        "This file defines the agent's personality and tone.\n"
        "The agent will embody whatever you write here.\n"
        "Edit this to customize how Vigil communicates with you.\n"
        "\n"
        "This file is loaded fresh each message -- no restart needed.\n"
        "Delete the contents (or this file) to use the default personality.\n"
        "-->"
    ),
)


def _normalize_soul(text: str) -> str:
    """Normalize SOUL.md content for legacy-template comparison."""
    # Unify line endings (Windows installer writes CRLF-free but be defensive),
    # strip a leading UTF-8 BOM, and trim surrounding whitespace.
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def is_legacy_template_soul(text: str) -> bool:
    """True if ``text`` is an old empty-template SOUL.md (no user persona).

    Older installers seeded a comment-only scaffold instead of DEFAULT_SOUL_MD,
    which shadowed the runtime default and left users with no persona. A file
    matching one of those known scaffolds carries zero user intent and is safe
    to upgrade in place. Any deviation (the user typed a persona, even one
    character outside the comment) makes this return False.
    """
    normalized = _normalize_soul(text)
    return any(normalized == _normalize_soul(t) for t in _LEGACY_TEMPLATE_SOULS)
