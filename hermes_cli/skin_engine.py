"""Vigil skin/theme engine — the theme SDK for every surface.

A data-driven skin system that lets users (and Vigil itself) customize the
visual appearance across the CLI, the TUI, and the desktop GUI from a single
file. Skins are defined as YAML files in ~/.vigil/skins/ or as built-in presets.
No code changes are needed to add a new skin.

This module is the source of truth: it resolves the active skin, and the gateway
pushes the resolved palette to the TUI and desktop (see tui_gateway's
``resolve_skin`` / ``skin.changed``). A skin dropped in ~/.vigil/skins/ therefore
themes all three surfaces at once — the theme analogue of the plugin SDK.

SKIN YAML SCHEMA
================

All fields are optional. Missing values inherit from the ``default`` skin.

.. code-block:: yaml

    # Required: skin identity
    name: mytheme                         # Unique skin name (lowercase, hyphens ok)
    description: Short description        # Shown in /skin listing

    # Colors: hex values for Rich markup (banner, UI, response box)
    colors:
      background: "#0e0e12"               # App/base surface — the seed the TUI
                                          # status bar and the desktop GUI derive
                                          # their whole palette from (see below).
      banner_border: "#CD7F32"            # Panel border color
      banner_title: "#FFD700"             # Panel title text color
      banner_accent: "#FFBF00"            # Section headers (Available Tools, etc.)
      banner_dim: "#B8860B"               # Dim/muted text (separators, labels)
      banner_text: "#FFF8DC"              # Body text (tool names, skill names)
      ui_accent: "#FFBF00"               # General UI accent
      ui_label: "#DAA520"                # UI labels (warm gold; teal clashed w/ default banner gold)
      ui_ok: "#4caf50"                   # Success indicators
      ui_error: "#ef5350"                # Error indicators
      ui_warn: "#ffa726"                 # Warning indicators
      ui_tool: "#FFBF00"                 # Tool-call markers (● / spinner); falls back to ui_accent
      ui_thinking: "#CC9B1F"             # Reasoning/thinking text; falls back to banner_dim
      diff_added: "#dcffdc"              # Diff added-line background (TUI)
      diff_removed: "#ffdcdc"            # Diff removed-line background
      diff_added_word: "#248a3d"         # Diff added word-level foreground
      diff_removed_word: "#cf222e"       # Diff removed word-level foreground
      syntax_string: "#FFBF00"           # Code strings; falls back to ui_accent
      syntax_number: "#FFF8DC"           # Code numbers; falls back to ui_text
      syntax_keyword: "#CD7F32"          # Code keywords; falls back to ui_border
      syntax_comment: "#CC9B1F"          # Code comments; falls back to banner_dim
      prompt: "#FFF8DC"                  # Prompt text color
      input_rule: "#CD7F32"              # Input area horizontal rule
      response_border: "#FFD700"         # Response box border (ANSI)
      status_bar_bg: "#1a1a2e"           # Status bar background
      status_bar_text: "#C0C0C0"         # Status bar default text
      status_bar_strong: "#FFD700"       # Status bar highlighted text
      status_bar_dim: "#8B8682"          # Status bar separators/muted text
      status_bar_good: "#8FBC8F"         # Healthy context usage
      status_bar_warn: "#FFD700"         # Warning context usage
      status_bar_bad: "#FF8C00"          # High context usage
      status_bar_critical: "#FF6B6B"     # Critical context usage
      session_label: "#DAA520"           # Session label color
      session_border: "#8B8682"          # Session ID dim color
      status_bar_bg: "#1a1a2e"          # TUI status/usage bar background
      voice_status_bg: "#1a1a2e"        # TUI voice status background
      selection_bg: "#333355"           # TUI mouse-selection highlight background
      completion_menu_bg: "#1a1a2e"      # Completion menu background
      completion_menu_current_bg: "#333355"  # Active completion row background
      completion_menu_meta_bg: "#1a1a2e"     # Completion meta column background
      completion_menu_meta_current_bg: "#333355"  # Active completion meta background

    # Optional paired palette for the opposite terminal polarity (mirrors the
    # desktop app's colors/darkColors pairing). If `colors` above is authored
    # for dark terminals, `light_colors` supplies the hand-tuned light-terminal
    # variant (same keys); light-authored skins supply `dark_colors` instead.
    # Without a paired block, the TUI adapts `colors` automatically
    # (contrast-clamped foregrounds, polarity-corrected fills).
    light_colors:
      banner_title: "#8B6914"
      # ... same keys as `colors` ...

    # Spinner: customize the animated spinner during API calls
    spinner:
      waiting_faces:                      # Faces shown while waiting for API
        - "(⚔)"
        - "(⛨)"
      thinking_faces:                     # Faces shown during reasoning
        - "(⌁)"
        - "(<>)"
      thinking_verbs:                     # Verbs for spinner messages
        - "forging"
        - "plotting"
      wings:                              # Optional left/right spinner decorations
        - ["⟪⚔", "⚔⟫"]                  # Each entry is [left, right] pair
        - ["⟪▲", "▲⟫"]

    # Branding: text strings used throughout the CLI
    branding:
      agent_name: "Vigil"                      # Banner title, status display
      welcome: "Welcome message"          # Shown at CLI startup
      goodbye: "Goodbye! ⚕"              # Shown on exit
      response_label: " ⚕ Vigil "       # Response box header label
      prompt_symbol: "❯"                 # Input prompt symbol (bare token; renderers add trailing space)
      help_header: "(^_^)? Commands"      # /help header text

    # Tool prefix: character for tool output lines (default: ┊)
    tool_prefix: "┊"

    # Tool emojis: override the default emoji for any tool (used in spinners & progress)
    tool_emojis:
      terminal: "⚔"           # Override terminal tool emoji
      web_search: "🔮"        # Override web_search tool emoji
      # Any tool not listed here uses its registry default

USAGE
=====

.. code-block:: python

    from hermes_cli.skin_engine import get_active_skin, list_skins, set_active_skin

    skin = get_active_skin()
    print(skin.colors["banner_title"])    # "#FFD700"
    print(skin.get_branding("agent_name"))  # "Vigil"

    set_active_skin("vigil")              # Switch to built-in vigil skin
    set_active_skin("mytheme")            # Switch to user skin from ~/.vigil/skins/

BUILT-IN SKINS
==============

- ``default`` — Vigil 蓝黑主题（与 vigil 同源，兼容旧 display.skin: default）
- ``vigil``   — Vigil ops 蓝黑主题（唯一内置真实皮肤；其余内置皮肤已删除）

未知皮肤名（含已删除的 ares/mono/slate/daylight/warm-lightmode/poseidon/
sisyphus/charizard）解析时回退 vigil 色值，不抛异常。

USER SKINS
==========

Drop a YAML file in ``~/.vigil/skins/<name>.yaml`` following the schema above.
Activate with ``/skin <name>`` in the CLI or ``display.skin: <name>`` in config.yaml.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_vigil_skin_dir

logger = logging.getLogger(__name__)


# =============================================================================
# Skin data structure
# =============================================================================

@dataclass
class SkinConfig:
    """Complete skin configuration."""
    name: str
    description: str = ""
    colors: Dict[str, str] = field(default_factory=dict)
    # Paired palettes for terminals whose background polarity differs from the
    # one `colors` was authored against (mirrors the desktop app's
    # colors/darkColors pairing). A consumer that knows the terminal is light
    # prefers `light_colors` (falling back to `colors`), and vice versa for
    # `dark_colors`. Both merge over the default skin's matching block, so
    # partial user skins still resolve to a complete palette.
    light_colors: Dict[str, str] = field(default_factory=dict)
    dark_colors: Dict[str, str] = field(default_factory=dict)
    spinner: Dict[str, Any] = field(default_factory=dict)
    branding: Dict[str, str] = field(default_factory=dict)
    tool_prefix: str = "┊"
    tool_emojis: Dict[str, str] = field(default_factory=dict)  # per-tool emoji overrides
    banner_logo: str = ""    # Rich-markup ASCII art logo (replaces HERMES_AGENT_LOGO)
    banner_hero: str = ""    # Rich-markup hero art (replaces HERMES_CADUCEUS)

    def get_color(self, key: str, fallback: str = "") -> str:
        """Get a color value with fallback."""
        return self.colors.get(key, fallback)

    def get_spinner_wings(self) -> List[Tuple[str, str]]:
        """Get spinner wing pairs, or empty list if none."""
        raw = self.spinner.get("wings", [])
        result = []
        for pair in raw:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                result.append((str(pair[0]), str(pair[1])))
        return result

    def get_branding(self, key: str, fallback: str = "") -> str:
        """Get a branding value with fallback."""
        return self.branding.get(key, fallback)


# =============================================================================
# Built-in skin definitions
# =============================================================================

_VIGIL_SKIN_DEFINITION: Dict[str, Any] = {
    "name": "vigil",
    "description": "Vigil ops theme — slate blue-gray on dark gray",
    # 蓝灰系 ops 主题：主色 #4A90D9，深色暗灰底（非纯黑）。层次阶梯：
    # 边框暗蓝灰 < 标题亮蓝灰 < accent 中蓝 < 正文浅灰白；工具指示亮蓝；
    # 状态栏中性灰蓝。banner 标识（◉ VIGIL）与欢迎语继承 default（品牌一致）。
    "colors": {
        "banner_border": "#3E6B9B",
        "banner_title": "#8FB8E8",
        "banner_accent": "#5B9BD5",
        "banner_dim": "#6B7F99",
        "banner_text": "#E8EEF5",
        "ui_accent": "#4A90D9",
        "ui_label": "#8FB8E8",
        "ui_ok": "#4CAF7D",
        "ui_error": "#E06C6C",
        "ui_warn": "#E0A060",
        "ui_tool": "#6BA9E8",
        "ui_thinking": "#7D93B8",
        "prompt": "#E8EEF5",
        "input_rule": "#3E6B9B",
        "response_border": "#4A90D9",
        "status_bar_bg": "#1A202A",
        "status_bar_text": "#C7D2E0",
        "status_bar_strong": "#8FB8E8",
        "status_bar_dim": "#5E6F87",
        "status_bar_good": "#5FB98A",
        "status_bar_warn": "#E0A060",
        "status_bar_bad": "#D9822B",
        "status_bar_critical": "#E06C6C",
        "session_label": "#8FB8E8",
        "session_border": "#5E6F87",
        "completion_menu_bg": "#1A202A",
        "completion_menu_current_bg": "#2A3B52",
        "completion_menu_meta_bg": "#1A202A",
        "completion_menu_meta_current_bg": "#30445E",
        "selection_bg": "#2A3B52",
        "shell_dollar": "#4A90D9",
        "voice_status_bg": "#1A202A",
        "syntax_string": "#7FC98C",
        "syntax_number": "#C7D2E0",
        "syntax_keyword": "#6BA9E8",
        "syntax_comment": "#5E6F87",
        # Ops env badge colors (consumed by the ops-mode CLI prompt/banner;
        # plain color keys, no schema change)
        "ops_env_test": "#4A90D9",
        "ops_env_uat": "#E0A060",
        "ops_env_prod": "#E06C6C",
    },
    "banner_logo": """[bold #8FB8E8]VIGIL[/]
[bold #5B9BD5]  /\_/\\[/]
[bold #8FB8E8]  ( ◉.◉ )[/]
[dim #6B7F99]  > ^ <[/]
[dim #6B7F99]记住整个平台，安全地动生产[/]""",
    "banner_hero": """[bold #8FB8E8]◉[/]
[bold #8FB8E8]VIGIL[/]
[dim #6B7F99]记住整个平台[/]
[dim #6B7F99]安全地动生产[/]""",
    "branding": {
        "prompt_symbol": "◉ Vigil >",
    },
    "spinner": {
        "waiting_faces": ["(·)", "(·|)", "(·/)", "(·\\)"],
        "thinking_faces": ["(·)", "(⌁)", "(∘)", "(○)"],
        "thinking_verbs": [
            "querying topology", "checking runbook", "verifying permissions",
            "tracing entity", "confirming env", "watching gates",
        ],
    },
    "tool_emojis": {
        "topo_query": "🧭",
        "topo_update": "🧭",
        "runbook_load": "📋",
        "runbook_checkpoint": "📋",
    },
    "tool_prefix": "┊",
}

# 内置皮肤只保留 Vigil 蓝黑一个真实主题（ares/mono/slate/daylight/
# warm-lightmode/poseidon/sisyphus/charizard 未适配 Vigil 设计，已删除）。
# ``default`` 键保留并与 vigil 同源，作为旧 ``display.skin: default`` 配置与
# 未知皮肤名的兜底——任何名字解析失败都回退 vigil 色值，不抛异常。
_BUILTIN_SKINS: Dict[str, Dict[str, Any]] = {
    "default": {
        **_VIGIL_SKIN_DEFINITION,
        "name": "default",
        "description": "Vigil 蓝黑主题（default 与 vigil 同源）",
        # 保留旧 default 的品牌键（agent_name/welcome/goodbye/...），消费方
        # 大多带 fallback，这里显式携带避免默认皮肤缺品牌文案。
        "branding": {
            **_VIGIL_SKIN_DEFINITION["branding"],
            "agent_name": "Vigil",
            "welcome": "Welcome to Vigil — topology loaded, runbooks ready, "
            "permission gates armed. Type a message or /help.",
            "goodbye": "Goodbye! ⚕",
            "response_label": " ◉ Vigil ",
            "help_header": "(^_^)? Available Commands",
        },
    },
    "vigil": _VIGIL_SKIN_DEFINITION,
}

# =============================================================================
# Skin loading and management
# =============================================================================

_active_skin: Optional[SkinConfig] = None
_active_skin_name: str = "default"


def _skins_dir() -> Path:
    """User skins directory."""
    return get_vigil_skin_dir()


def _load_skin_from_yaml(path: Path) -> Optional[Dict[str, Any]]:
    """Load a skin definition from a YAML file."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "name" in data:
            return data
    except Exception as e:
        logger.debug("Failed to load skin from %s: %s", path, e)
    return None


def _mapping_or_empty(value: Any, *, section: str, skin_name: str) -> Dict[str, Any]:
    """Return a mapping value or an empty dict when the section type is invalid."""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    logger.warning(
        "Skin '%s' has invalid '%s' section type (%s); ignoring section",
        skin_name,
        section,
        type(value).__name__,
    )
    return {}


def _build_skin_config(data: Dict[str, Any]) -> SkinConfig:
    """Build a SkinConfig from a raw dict (built-in or loaded from YAML)."""
    # Start with default values as base for missing keys
    default = _BUILTIN_SKINS["default"]
    skin_name = str(data.get("name", "unknown"))
    color_overrides = _mapping_or_empty(data.get("colors"), section="colors", skin_name=skin_name)
    spinner_overrides = _mapping_or_empty(data.get("spinner"), section="spinner", skin_name=skin_name)
    branding_overrides = _mapping_or_empty(data.get("branding"), section="branding", skin_name=skin_name)
    emoji_overrides = _mapping_or_empty(data.get("tool_emojis"), section="tool_emojis", skin_name=skin_name)

    colors = dict(default.get("colors", {}))
    colors.update(color_overrides)
    spinner = dict(default.get("spinner", {}))
    spinner.update(spinner_overrides)
    branding = dict(default.get("branding", {}))
    branding.update(branding_overrides)

    # Paired palettes are NOT merged over the default skin's blocks: an empty
    # block means "this skin has no hand-tuned variant for that polarity", and
    # consumers (the TUI) fall back to `colors` + automatic adaptation. Merging
    # the default's gold light palette under a crimson skin would be worse
    # than adapting the crimson.
    light_colors = _mapping_or_empty(data.get("light_colors"), section="light_colors", skin_name=skin_name)
    dark_colors = _mapping_or_empty(data.get("dark_colors"), section="dark_colors", skin_name=skin_name)

    return SkinConfig(
        name=skin_name,
        description=data.get("description", ""),
        colors=colors,
        light_colors=light_colors,
        dark_colors=dark_colors,
        spinner=spinner,
        branding=branding,
        tool_prefix=data.get("tool_prefix", default.get("tool_prefix", "┊")),
        tool_emojis=emoji_overrides,
        banner_logo=data.get("banner_logo", ""),
        banner_hero=data.get("banner_hero", ""),
    )


def list_skins() -> List[Dict[str, str]]:
    """List all available skins (built-in + user-installed).

    Returns list of {"name": ..., "description": ..., "source": "builtin"|"user"}.
    """
    result = []
    for name, data in _BUILTIN_SKINS.items():
        result.append({
            "name": name,
            "description": data.get("description", ""),
            "source": "builtin",
        })

    skins_path = _skins_dir()
    if skins_path.is_dir():
        for f in sorted(skins_path.glob("*.yaml")):
            data = _load_skin_from_yaml(f)
            if data:
                skin_name = data.get("name", f.stem)
                # Skip if it shadows a built-in
                if any(s["name"] == skin_name for s in result):
                    continue
                result.append({
                    "name": skin_name,
                    "description": data.get("description", ""),
                    "source": "user",
                })

    return result


_UNKNOWN_SKIN_WARNED: set[str] = set()


def _warn_unknown_skin_once(name: str) -> None:
    """Warn once per unknown skin name (deleted built-ins included)."""
    if name in _UNKNOWN_SKIN_WARNED:
        return
    _UNKNOWN_SKIN_WARNED.add(name)
    logger.warning(
        "Skin '%s' not found, falling back to Vigil blue-black (default)", name
    )


def load_skin(name: str) -> SkinConfig:
    """Load a skin by name. Checks user skins first, then built-in.

    未知名字（含已删除的内置皮肤，如 slate）→ 回退 vigil 色值，不抛异常。
    """
    # Check user skins directory
    skins_path = _skins_dir()
    user_file = skins_path / f"{name}.yaml"
    if user_file.is_file():
        data = _load_skin_from_yaml(user_file)
        if data:
            return _build_skin_config(data)

    # Check built-in skins
    if name in _BUILTIN_SKINS:
        return _build_skin_config(_BUILTIN_SKINS[name])

    # Fallback: any unknown name resolves to the Vigil blue-black palette.
    _warn_unknown_skin_once(name)
    return _build_skin_config(_BUILTIN_SKINS["default"])


def get_active_skin() -> SkinConfig:
    """Get the currently active skin config (cached)."""
    global _active_skin
    if _active_skin is None:
        _active_skin = load_skin(_active_skin_name)
    return _active_skin


def set_active_skin(name: str) -> SkinConfig:
    """Switch the active skin. Returns the new SkinConfig."""
    global _active_skin, _active_skin_name
    _active_skin_name = name
    _active_skin = load_skin(name)
    return _active_skin


def get_active_skin_name() -> str:
    """Get the name of the currently active skin."""
    return _active_skin_name


def init_skin_from_config(config: dict) -> None:
    """Initialize the active skin from CLI config at startup.

    Call this once during CLI init with the loaded config dict.
    """
    display = config.get("display") or {}
    if not isinstance(display, dict):
        display = {}
    skin_name = display.get("skin", "default")
    if isinstance(skin_name, str) and skin_name.strip():
        set_active_skin(skin_name.strip())
    else:
        set_active_skin("default")


# =============================================================================
# Convenience helpers for CLI modules
# =============================================================================


def get_active_prompt_symbol(fallback: str = "❯") -> str:
    """Return the interactive prompt symbol with a single trailing space.

    Skins store ``prompt_symbol`` as a bare token (no spaces). The trailing
    space is appended here so callers can drop it straight into a rendered
    prompt without hand-rolling whitespace.
    """
    try:
        raw = get_active_skin().get_branding("prompt_symbol", fallback)
    except Exception:
        raw = fallback

    cleaned = (raw or fallback).strip()

    return f"{cleaned or fallback.strip()} "



def get_active_help_header(fallback: str = "(^_^)? Available Commands") -> str:
    """Get the /help header from the active skin."""
    try:
        return get_active_skin().get_branding("help_header", fallback)
    except Exception:
        return fallback



def get_active_goodbye(fallback: str = "Goodbye! ⚕") -> str:
    """Get the goodbye line from the active skin."""
    try:
        return get_active_skin().get_branding("goodbye", fallback)
    except Exception:
        return fallback



def get_prompt_toolkit_style_overrides() -> Dict[str, str]:
    """Return prompt_toolkit style overrides derived from the active skin.

    These are layered on top of the CLI's base TUI style so /skin can refresh
    the live prompt_toolkit UI immediately without rebuilding the app.
    """
    try:
        skin = get_active_skin()
    except Exception:
        return {}

    # Input/prompt: leave unset by default so the typed text inherits
    # the terminal's foreground color (readable in both light and dark
    # color schemes).  Skins can opt into a colored prompt by setting
    # `prompt` explicitly in their YAML.
    prompt = skin.get_color("prompt", "")
    input_rule = skin.get_color("input_rule", "#CD7F32")
    title = skin.get_color("banner_title", "#FFD700")
    text = skin.get_color("banner_text", "#FFF8DC")
    dim = skin.get_color("banner_dim", "#555555")
    label = skin.get_color("ui_label", title)
    warn = skin.get_color("ui_warn", "#FF8C00")
    error = skin.get_color("ui_error", "#FF6B6B")
    status_bg = skin.get_color("status_bar_bg", "#1a1a2e")
    status_text = skin.get_color("status_bar_text", text)
    status_strong = skin.get_color("status_bar_strong", title)
    status_dim = skin.get_color("status_bar_dim", dim)
    status_good = skin.get_color("status_bar_good", skin.get_color("ui_ok", "#8FBC8F"))
    status_warn = skin.get_color("status_bar_warn", warn)
    status_bad = skin.get_color("status_bar_bad", skin.get_color("banner_accent", warn))
    status_critical = skin.get_color("status_bar_critical", error)
    env_test = skin.get_color("ops_env_test", "#4A90D9")
    env_uat = skin.get_color("ops_env_uat", "#E0A060")
    env_prod = skin.get_color("ops_env_prod", "#E06C6C")
    voice_bg = skin.get_color("voice_status_bg", status_bg)
    menu_bg = skin.get_color("completion_menu_bg", "#1a1a2e")
    menu_current_bg = skin.get_color("completion_menu_current_bg", "#333355")
    menu_meta_bg = skin.get_color("completion_menu_meta_bg", menu_bg)
    menu_meta_current_bg = skin.get_color("completion_menu_meta_current_bg", menu_current_bg)

    return {
        # Typed input always uses terminal default fg/bg so it's
        # readable in both light and dark Terminal.app modes.  The
        # skin's `prompt` color (if any) only styles the prompt symbol,
        # NOT the user's typed text.
        "input-area": "",
        "placeholder": f"{dim} italic",
        "prompt": prompt,
        "prompt-working": f"{dim} italic",
        "hint": f"{dim} italic",
        "status-bar": f"bg:{status_bg} {status_text}",
        "status-bar-strong": f"bg:{status_bg} {status_strong} bold",
        "status-bar-dim": f"bg:{status_bg} {status_dim}",
        "status-bar-good": f"bg:{status_bg} {status_good} bold",
        "status-bar-warn": f"bg:{status_bg} {status_warn} bold",
        "status-bar-bad": f"bg:{status_bg} {status_bad} bold",
        "status-bar-critical": f"bg:{status_bg} {status_critical} bold",
        "input-rule": input_rule,
        "image-badge": f"{label} bold",
        "ops-env-test": f"{env_test} bold",
        "ops-env-uat": f"{env_uat} bold",
        "ops-env-prod": f"{env_prod} bold",
        "completion-menu": f"bg:{menu_bg} {text}",
        "completion-menu.completion": f"bg:{menu_bg} {text}",
        "completion-menu.completion.current": f"bg:{menu_current_bg} {title}",
        "completion-menu.meta.completion": f"bg:{menu_meta_bg} {dim}",
        "completion-menu.meta.completion.current": f"bg:{menu_meta_current_bg} {label}",
        "clarify-border": input_rule,
        "clarify-title": f"{title} bold",
        "clarify-question": f"{text} bold",
        "clarify-choice": dim,
        "clarify-selected": f"{title} bold",
        "clarify-active-other": f"{title} italic",
        "clarify-countdown": input_rule,
        "sudo-prompt": f"{error} bold",
        "sudo-border": input_rule,
        "sudo-title": f"{error} bold",
        "sudo-text": text,
        "approval-border": input_rule,
        "approval-title": f"{warn} bold",
        "approval-desc": f"{text} bold",
        "approval-cmd": f"{dim} italic",
        "approval-choice": dim,
        "approval-selected": f"{title} bold",
        "voice-status": f"bg:{voice_bg} {label}",
        "voice-status-recording": f"bg:{voice_bg} {error} bold",
    }
