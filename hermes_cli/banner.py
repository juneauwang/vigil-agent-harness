"""Welcome banner, ASCII art, skills summary, and update check for the CLI.

Pure display functions with no HermesCLI state dependency.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from hermes_constants import get_hermes_home
from typing import TYPE_CHECKING, Any, Dict, List, Optional

# rich and prompt_toolkit are imported lazily (inside the functions that use
# them) rather than at module level.  Importing this module is on the TUI
# gateway's critical startup path purely to reach the lightweight update-check
# helpers (``prefetch_update_check``); pulling rich.console + prompt_toolkit
# eagerly added ~50ms of wasted imports before ``gateway.ready`` could fire.
# Keep the type-only reference available to checkers without the runtime cost.
if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)


# =========================================================================
# ANSI building blocks for conversation display
# =========================================================================

_GOLD = "\033[1;38;2;255;215;0m"  # True-color #FFD700 bold
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        # prompt_toolkit needs a real console. On Windows, a redirected or
        # absent stdout (pythonw.exe, CI, `hermes ... > file`) raises
        # NoConsoleScreenBufferError from its Win32Output — display helpers
        # must never crash the caller over that, so degrade to plain print.
        print(text)


# =========================================================================
# Skin-aware color helpers
# =========================================================================

def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from hermes_cli.skin_engine import get_active_skin
        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback
# =========================================================================
# ASCII Art & Branding
# =========================================================================

from hermes_cli import __version__ as VERSION, __release_date__ as RELEASE_DATE

def _vigil_owl_lines():
    """Owl mascot art as (region, text) rows; backslashes are literal text.

    Rows are colored by region when rendered: accent = ears, bright = eyes,
    dim = beak/chin.
    """
    bs = chr(92)
    return [
        ("accent", f"  /{bs}_/{bs}"),
        ("bright", "  ( \u25c9.\u25c9 )"),
        ("dim",    "  > ^ <"),
    ]
def _vigil_owl_art(accent: str, bright: str, dim: str) -> str:
    """Owl mascot (3 rows) as colored Rich markup, no wordmark or tagline."""
    parts = []
    for region, text in _vigil_owl_lines():
        color = {"accent": accent, "bright": bright, "dim": dim}[region]
        style = f"bold {color}" if region != "dim" else color
        escaped = text[:-1] + "\\\\" if text.endswith("\\") else text
        parts.append(f"[{style}]{escaped}[/]")
    return "\n".join(parts)



def get_vigil_owl_markup(accent: Optional[str] = None, bright: Optional[str] = None,
                         dim: Optional[str] = None) -> str:
    """Return the Vigil owl wordmark block (VIGIL + owl + tagline) as Rich markup.

    Blue-gray brand theme by default; adapts to whatever skin is active so the
    owl stays legible on any theme. Used by /help; the startup banner renders
    the bare owl via _vigil_owl_art() inside its status panel.
    """
    if None in (accent, bright, dim):
        try:
            from hermes_cli.skin_engine import get_active_skin
            _s = get_active_skin()
            accent = accent or _s.get_color("banner_accent", "#5B9BD5")
            bright = bright or _s.get_color("banner_title", "#8FB8E8")
            dim = dim or _s.get_color("banner_dim", "#6B7F99")
        except Exception:
            pass
    accent = accent or "#5B9BD5"
    bright = bright or "#8FB8E8"
    dim = dim or "#6B7F99"
    return "\n".join([
        f"[bold {bright}]VIGIL[/]",
        _vigil_owl_art(accent, bright, dim),
        f"[dim {dim}]\u8bb0\u4f4f\u6574\u4e2a\u5e73\u53f0\uff0c\u5b89\u5168\u5730\u52a8\u751f\u4ea7[/]",
    ])


VIGIL_LOGO = get_vigil_owl_markup("#5B9BD5", "#8FB8E8", "#6B7F99")





# =========================================================================
# Skills scanning
# =========================================================================

def get_available_skills() -> Dict[str, List[str]]:
    """Return skills grouped by category, filtered by platform and disabled state.

    Delegates to ``_find_all_skills()`` from ``tools/skills_tool`` which already
    handles platform gating (``platforms:`` frontmatter) and respects the
    user's ``skills.disabled`` config list.
    """
    try:
        from tools.skills_tool import _find_all_skills
        all_skills = _find_all_skills()  # already filtered
    except Exception:
        return {}

    skills_by_category: Dict[str, List[str]] = {}
    for skill in all_skills:
        category = skill.get("category") or "general"
        skills_by_category.setdefault(category, []).append(skill["name"])
    return skills_by_category


# =========================================================================
# Update check
# =========================================================================

# Cache update check results for 6 hours to avoid repeated git fetches
_UPDATE_CHECK_CACHE_SECONDS = 6 * 3600

# Sentinel returned when we know an update exists but can't count commits
# (e.g. nix-built hermes — no local git history to count against).
UPDATE_AVAILABLE_NO_COUNT = -1

_UPSTREAM_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
_OFFICIAL_REPO_CANONICAL = "github.com/nousresearch/hermes-agent"


def _canonical_github_remote(url: str | None) -> str:
    """Return ``host/owner/repo`` for common GitHub remote URL forms."""
    if not url:
        return ""
    value = url.strip()
    if value.startswith("git@github.com:"):
        value = "github.com/" + value[len("git@github.com:"):]
    elif value.startswith("ssh://git@github.com/"):
        value = "github.com/" + value[len("ssh://git@github.com/"):]
    else:
        parsed = urlparse(value)
        if parsed.netloc and parsed.path:
            value = f"{parsed.netloc}{parsed.path}"
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()


def _is_ssh_remote(url: str | None) -> bool:
    if not url:
        return False
    value = url.strip().lower()
    return value.startswith("git@") or value.startswith("ssh://")


def _is_official_ssh_remote(url: str | None) -> bool:
    return _is_ssh_remote(url) and _canonical_github_remote(url) == _OFFICIAL_REPO_CANONICAL


def _git_stdout(args: list[str], *, cwd: Path, timeout: int = 5) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            # git output is UTF-8; on Windows text=True defaults to the ANSI
            # code page and bytes like 0x90 (3rd byte of 🐛 in a commit
            # subject) crash the stdlib reader thread (#52649).
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _check_via_rev(local_rev: str) -> Optional[int]:
    """Compare an embedded git revision to upstream main via ls-remote.

    Returns 0 if up-to-date, ``UPDATE_AVAILABLE_NO_COUNT`` if behind,
    or ``None`` on failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", _UPSTREAM_REPO_URL, "refs/heads/main"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    upstream_rev = result.stdout.split()[0]
    if not upstream_rev:
        return None
    return 0 if upstream_rev == local_rev else UPDATE_AVAILABLE_NO_COUNT


def _check_via_local_git(repo_dir: Path) -> Optional[int]:
    """Count commits behind origin/main in a local checkout."""
    origin_url = _git_stdout(["remote", "get-url", "origin"], cwd=repo_dir)
    if _is_official_ssh_remote(origin_url):
        head_rev = _git_stdout(["rev-parse", "HEAD"], cwd=repo_dir)
        checked = _check_via_rev(head_rev) if head_rev else None
        if checked == UPDATE_AVAILABLE_NO_COUNT:
            return 1
        return checked

    # Installer checkouts are shallow (`git clone --depth 1`). On a shallow
    # clone the history stops at a single commit, so a plain `git fetch` would
    # unshallow the repo (dragging in the whole history) and
    # `rev-list --count HEAD..origin/main` would report a huge bogus "behind"
    # number (e.g. "12492 commits behind"). Detect shallow up front: fetch with
    # --depth 1 to preserve the boundary and compare tip SHAs instead of
    # counting. Full clones (developers, Docker dev images) keep the exact
    # count path unchanged. Mirrors the desktop fix in apps/desktop/electron/main.cjs.
    shallow = _git_stdout(["rev-parse", "--is-shallow-repository"], cwd=repo_dir)
    is_shallow = shallow == "true"

    try:
        # Scope the fetch to the one branch the behind-count compares against.
        # An unscoped ``git fetch origin`` transfers every remote head (~1,400
        # on this repo — measured 3.0 s vs 0.55 s scoped) and can burn the full
        # 10 s timeout on slow links. ``cmd_update`` already scopes its fetch
        # for the same reason. Modern git updates the ``origin/main`` tracking
        # ref on a scoped fetch, so the ``HEAD..origin/main`` count below is
        # unaffected; the shallow path compares against FETCH_HEAD, which a
        # scoped fetch also updates.
        fetch_args = ["git", "fetch", "origin", "main"]
        if is_shallow:
            fetch_args += ["--depth", "1"]
        fetch_args.append("--quiet")
        subprocess.run(
            fetch_args,
            capture_output=True, timeout=10,
            cwd=str(repo_dir),
        )
    except Exception:
        pass  # Offline or timeout — use stale refs, that's fine

    if is_shallow:
        # No history to count across the shallow boundary. `origin/main` may not
        # be a tracking ref in a `clone --depth 1`, so prefer FETCH_HEAD (just
        # updated by the fetch above) and fall back to origin/main.
        head_rev = _git_stdout(["rev-parse", "HEAD"], cwd=repo_dir)
        target_rev = (
            _git_stdout(["rev-parse", "FETCH_HEAD"], cwd=repo_dir)
            or _git_stdout(["rev-parse", "origin/main"], cwd=repo_dir)
        )
        if not head_rev or not target_rev:
            return None
        return 0 if head_rev == target_rev else UPDATE_AVAILABLE_NO_COUNT

    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def check_for_updates() -> Optional[int]:
    """Check whether a Vigil update is available.

    Two paths: if ``HERMES_REVISION`` is set (nix builds embed it), compare
    it to upstream main via ``git ls-remote``. Otherwise look for a local
    git checkout and count commits behind ``origin/main``.

    Returns the number of commits behind, ``UPDATE_AVAILABLE_NO_COUNT`` (-1)
    if behind but the count is unknown, ``0`` if up-to-date, or ``None`` if
    the check failed or doesn't apply. Cached for 6 hours.
    """
    hermes_home = get_hermes_home()
    cache_file = hermes_home / ".update_check"
    embedded_rev = os.environ.get("HERMES_REVISION") or None

    # Docker images have no working tree to count commits against — the
    # published image excludes `.git` (see .dockerignore) and sets no
    # HERMES_REVISION (that's nix-only). Returning None makes both the Rich
    # banner (build_welcome_banner) and the Ink badge (branding.tsx, guarded
    # on `typeof === 'number' && > 0`) show nothing. The dashboard's REST
    # `/api/hermes/update/check` endpoint short-circuits docker the same way
    # (web_server.py); mirror that here so the banner/TUI surfaces agree.
    try:
        from hermes_cli.config import detect_install_method, get_project_root
        if detect_install_method(get_project_root()) == "docker":
            return None
    except Exception:
        pass

    # Read cache — invalidate if the embedded rev OR installed version has
    # changed since the last check.
    now = time.time()
    try:
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (
                now - cached.get("ts", 0) < _UPDATE_CHECK_CACHE_SECONDS
                and cached.get("rev") == embedded_rev
                and cached.get("ver") == VERSION
            ):
                return cached.get("behind")
    except Exception:
        pass

    if embedded_rev:
        behind = _check_via_rev(embedded_rev)
    else:
        # Prefer the running code's location over the profile-scoped path.
        # $HERMES_HOME/hermes-agent/ may be a stale copy from --clone-all;
        # Path(__file__) always resolves to the actual installed checkout.
        repo_dir = Path(__file__).parent.parent.resolve()
        if not (repo_dir / ".git").exists():
            repo_dir = hermes_home / "hermes-agent"
        if not (repo_dir / ".git").exists():
            # No git checkout and no embedded revision — can't determine
            # update status. This is the Docker path (already short-circuited
            # above) or an unsupported install without a source tree.
            behind = None
        else:
            behind = _check_via_local_git(repo_dir)

    try:
        cache_file.write_text(
            json.dumps({"ts": now, "behind": behind, "rev": embedded_rev, "ver": VERSION}),
            encoding="utf-8",
        )
    except Exception:
        pass

    return behind


def _resolve_repo_dir() -> Optional[Path]:
    """Return the active Vigil git checkout, or None if this isn't a git install.

    Prefers the running code's location over the profile-scoped path
    because ``$HERMES_HOME/hermes-agent/`` may be a stale copy carried
    over by ``--clone-all``.
    """
    repo_dir = Path(__file__).parent.parent.resolve()
    if not (repo_dir / ".git").exists():
        hermes_home = get_hermes_home()
        repo_dir = hermes_home / "hermes-agent"
    return repo_dir if (repo_dir / ".git").exists() else None


def _git_short_hash(repo_dir: Path, rev: str) -> Optional[str]:
    """Resolve a git revision to an 8-character short hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", rev],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def get_git_banner_state(repo_dir: Optional[Path] = None) -> Optional[dict]:
    """Return upstream/local git hashes for the startup banner.

    For source installs and dev images this runs ``git rev-parse`` against
    the active checkout.  When no checkout is available — the canonical case
    is the published Docker image, which excludes ``.git`` from the build
    context — we fall back to the baked-in build SHA (see
    ``hermes_cli/build_info.py``) and return it as a frozen
    ``upstream == local`` state with ``ahead=0``.  A built image is by
    definition pinned to one commit, so "ahead" is always zero and the
    banner correctly shows ``· upstream <sha>`` with no carried-commits
    annotation.
    """
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        # No git checkout — try the baked build SHA (Docker image path).
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {"upstream": baked, "local": baked, "ahead": 0}
        except Exception:
            pass
        return None

    upstream = _git_short_hash(repo_dir, "origin/main")
    local = _git_short_hash(repo_dir, "HEAD")
    if not upstream or not local:
        # Live-git lookup failed (e.g. shallow clone without origin/main).
        # Fall back to the baked build SHA if available.
        try:
            from hermes_cli.build_info import get_build_sha
            baked = get_build_sha(short=8)
            if baked:
                return {"upstream": baked, "local": baked, "ahead": 0}
        except Exception:
            pass
        return None

    ahead = 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            ahead = int((result.stdout or "0").strip() or "0")
    except Exception:
        ahead = 0

    return {"upstream": upstream, "local": local, "ahead": max(ahead, 0)}


_RELEASE_URL_BASE = "https://github.com/NousResearch/hermes-agent/releases/tag"
_latest_release_cache: Optional[tuple] = None  # (tag, url) once resolved


def get_latest_release_tag(repo_dir: Optional[Path] = None) -> Optional[tuple]:
    """Return ``(tag, release_url)`` for the latest git tag, or None.

    Local-only — runs ``git describe --tags --abbrev=0`` against the
    Vigil checkout. Cached per-process. Release URL always points at the
    canonical NousResearch/hermes-agent repo (forks don't get a link).
    """
    global _latest_release_cache
    if _latest_release_cache is not None:
        return _latest_release_cache or None

    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        _latest_release_cache = ()  # falsy sentinel — skip future lookups
        return None

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            cwd=str(repo_dir),
        )
    except Exception:
        _latest_release_cache = ()
        return None

    if result.returncode != 0:
        _latest_release_cache = ()
        return None

    tag = (result.stdout or "").strip()
    if not tag:
        _latest_release_cache = ()
        return None

    url = f"{_RELEASE_URL_BASE}/{tag}"
    _latest_release_cache = (tag, url)
    return _latest_release_cache


# Short-TTL cache: the prompt renderer probes this on every repaint; the
# snapshot is cheap but shouldn't hit config.yaml + filesystem per frame.
_banner_state_cache: Optional[tuple] = None  # (monotonic_ts, state)
_BANNER_STATE_TTL = 5.0


def _load_banner_state() -> Dict[str, Any]:
    """Return the unified banner snapshot (always a dict).

    Reads the ops config block (config.yaml) and the profile home
    (topology.yaml / runbooks/) — display-only, no agent involvement.
    ``ops_enabled`` is True when any ops capability is active; without it the
    same status rows render their off state (unified console header for every
    profile — one visual language, not an ops/non-ops split).
    """
    global _banner_state_cache
    _now = time.monotonic()
    if _banner_state_cache is not None and _now - _banner_state_cache[0] < _BANNER_STATE_TTL:
        return _banner_state_cache[1]

    state: Dict[str, Any] = {
        "ops_enabled": False,
        "env": "",
        "matrix_enabled": False,
        "topology_enabled": False,
        "runbook_enabled": False,
        "entity_count": 0,
        "runbook_count": 0,
        "profile": "",
        "home": "",
    }
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name()
        if profile and profile != "default":
            state["profile"] = profile
    except Exception:
        pass
    home = None
    try:
        home = Path(get_hermes_home())
        state["home"] = str(home)
    except Exception:
        pass

    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        ops = cfg.get("ops") or {}
        if isinstance(ops, dict):
            topology = ops.get("topology") or {}
            permissions = ops.get("permissions") or {}
            runbooks = ops.get("runbooks") or {}
            state["ops_enabled"] = bool(
                topology.get("enabled") or permissions.get("enabled")
            )
            state["matrix_enabled"] = bool(permissions.get("enabled"))
            state["topology_enabled"] = bool(topology.get("enabled"))
            state["runbook_enabled"] = bool(runbooks.get("enabled"))
            env = str(permissions.get("env") or "").strip()
            if env:
                state["env"] = env
    except Exception:
        pass

    if state["ops_enabled"] and home is not None:
        try:
            topo_file = home / "topology.yaml"
            if topo_file.is_file():
                import yaml
                data = yaml.safe_load(topo_file.read_text(encoding="utf-8")) or {}
                entities = data.get("core_entities") or []
                if isinstance(entities, list):
                    state["entity_count"] = len(entities)
            runbooks_dir = home / "runbooks"
            if runbooks_dir.is_dir():
                state["runbook_count"] = len(list(runbooks_dir.glob("*.yaml")))
        except Exception:
            pass

    _banner_state_cache = (_now, state)
    return state


def _render_banner(console, *, model: str, cwd: str, session_id: Optional[str],
                   context_length: Optional[int], provider: Optional[str],
                   state: dict, tools: list, enabled_toolsets: list) -> None:
    """Render the unified Vigil console header (every profile, one language).

    Left: hero mark + session anchor. Right: PROFILE / ENV / GATES /
    TOPOLOGY / RUNBOOKS / HOME status — ops capabilities show their live
    state when enabled, an explicit off state otherwise. Bottom: a single
    capability line (ops tool anchors, or a compact tool/skill summary).
    No assistant-style tool/skill inventory.
    """
    from rich.panel import Panel
    from rich.table import Table

    try:
        from hermes_cli.skin_engine import get_active_skin
        _skin = get_active_skin()

        def _c(key: str, fallback: str) -> str:
            return _skin.get_color(key, fallback)
    except Exception:
        _c = lambda key, fallback: fallback

    accent = _c("banner_accent", "#FFBF00")
    dim = _c("banner_dim", "#B8860B")
    text = _c("banner_text", "#FFF8DC")
    title_color = _c("banner_title", "#FFD700")
    border_color = _c("banner_border", "#CD7F32")
    ok = _c("ui_ok", "#4caf50")
    err = _c("ui_error", "#ef5350")
    warn = _c("ui_warn", "#ffa726")
    session_color = _c("session_border", "#8B8682")

    # Left column: hero mark + session anchor
    left_lines = ["", _vigil_owl_art(accent, text, dim), ""]
    if (provider or "").strip().lower() == "moa":
        # MoA virtual provider: ``model`` is a preset name. Show the preset and
        # its aggregator so the banner is meaningful instead of a bare slug.
        preset_name = model
        agg_label = ""
        try:
            from hermes_cli.config import load_config
            from hermes_cli.moa_config import normalize_moa_config
            _moa = normalize_moa_config(load_config().get("moa") or {})
            _preset = _moa.get("presets", {}).get(preset_name)
            if _preset:
                _agg = _preset.get("aggregator") or {}
                _am = str(_agg.get("model") or "")
                agg_label = _am.split("/")[-1] if "/" in _am else _am
        except Exception:
            agg_label = ""
        if len(preset_name) > 28:
            preset_name = preset_name[:25] + "..."
        agg_str = f" [dim {dim}]·[/] [dim {dim}]agg {agg_label}[/]" if agg_label else ""
        ctx_str = f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]" if context_length else ""
        left_lines.append(f"[{accent}]MoA: {preset_name}[/]{agg_str}{ctx_str}")
    else:
        if not (model or "").strip() or (model or "").strip().lower() == "unknown":
            # Unconfigured install: say so in red instead of a blank/"unknown"
            # slug — this is the single clearest place to tell the user what
            # is wrong and how to fix it.
            left_lines.append(
                f"[bold red]no model configured[/] "
                f"[dim {dim}]— run /model or vigil setup[/]"
            )
        else:
            model_short = model.split("/")[-1] if "/" in model else model
            if model_short.endswith(".gguf"):
                model_short = model_short[:-5]
            if len(model_short) > 28:
                model_short = model_short[:25] + "..."
            ctx_str = f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]" if context_length else ""
            left_lines.append(f"[{accent}]{model_short}[/]{ctx_str}")
    if os.getenv("HERMES_YOLO_MODE"):
        left_lines.append(f"[bold red]⚠ YOLO mode[/] [dim {dim}]— all approval prompts bypassed[/]")
    left_lines.append(f"[dim {dim}]{cwd}[/]")
    if session_id:
        left_lines.append(f"[dim {session_color}]Session: {session_id}[/]")
    left_content = "\n".join(left_lines)

    # Right column: unified status — where am I / what does Vigil remember / gates
    ops_on = bool(state.get("ops_enabled"))
    right_lines: List[str] = []
    profile_name = str(state.get("profile") or "default")
    right_lines.append(f"[dim {dim}]PROFILE[/]     [{text}]{profile_name}[/]")
    if ops_on:
        env = str(state.get("env") or "unknown")
        env_color = _c(f"ops_env_{env}", "#4A90D9")
        env_badge = f"[{env_color} bold]\\[{env}][/]" if env_color else f"[bold]\\[{env}][/]"
    else:
        env_badge = f"[dim {dim}]—[/]"
    right_lines.append(f"[dim {dim}]ENV[/]         {env_badge}")
    if ops_on and state.get("matrix_enabled"):
        right_lines.append(
            f"[dim {dim}]GATES[/]       [{ok} bold]matrix ON[/] "
            f"[dim {dim}]· L1–L4 × env → execute / approve / deny[/]"
        )
    elif ops_on:
        right_lines.append(
            f"[dim {dim}]GATES[/]       [{err} bold]matrix OFF[/] "
            f"[dim {dim}]· fail-closed 失效[/]"
        )
    else:
        right_lines.append(f"[dim {dim}]GATES[/]       [dim {dim}]off（未启用 ops harness）[/]")
    if ops_on and state.get("topology_enabled"):
        if state.get("entity_count"):
            topo_val = f"[{text}]{state.get('entity_count', 0)} entities[/]"
        else:
            topo_val = f"[{warn}]no topology loaded[/]"
        right_lines.append(f"[dim {dim}]TOPOLOGY[/]    {topo_val}")
    else:
        right_lines.append(f"[dim {dim}]TOPOLOGY[/]    [dim {dim}]—[/]")
    if ops_on and state.get("runbook_enabled"):
        right_lines.append(
            f"[dim {dim}]RUNBOOKS[/]    [{text}]{state.get('runbook_count', 0)} loaded[/]"
        )
    else:
        right_lines.append(f"[dim {dim}]RUNBOOKS[/]    [dim {dim}]—[/]")
    home_disp = str(state.get("home") or "")
    try:
        home_disp = home_disp.replace(str(Path.home()), "~")
    except Exception:
        pass
    right_lines.append(f"[dim {dim}]HOME[/]        [dim {dim}]{home_disp or '—'}[/]")
    right_lines.append("")
    if ops_on:
        right_lines.append(
            f"[dim {dim}]◈ topo_query  ·  runbook_load  ·  permission matrix  ·  /help for commands[/]"
        )
        if not state.get("matrix_enabled"):
            right_lines.append(
                f"[bold {err}]⚠ 权限矩阵未启用——安全门已打开，请立即在 config.yaml 启用 ops.permissions[/]"
            )
    else:
        try:
            total_skills = sum(len(v) for v in get_available_skills().values())
        except Exception:
            total_skills = 0
        right_lines.append(
            f"[dim {dim}]◈ {len(tools)} tools · {total_skills} skills · /help for commands[/]"
        )
        right_lines.append(
            f"[dim {dim}]  提示：运行 scripts/ops_init.py 启用运维能力（拓扑表 + runbook + 权限矩阵）[/]"
        )
    # Update check — use prefetched result if available
    try:
        behind = get_update_result(timeout=0.5)
        if behind is not None and behind != 0:
            from hermes_cli.config import get_managed_update_command, recommended_update_command
            if behind > 0:
                commits_word = "commit" if behind == 1 else "commits"
                right_lines.append(
                    f"[bold yellow]⚠ {behind} {commits_word} behind[/]"
                    f"[dim yellow] — run [bold]{recommended_update_command()}[/bold] to update[/]"
                )
            else:
                managed_cmd = get_managed_update_command()
                line = "[bold yellow]⚠ update available[/]"
                if managed_cmd:
                    line += f"[dim yellow] — run [bold]{managed_cmd}[/bold][/]"
                right_lines.append(line)
    except Exception:
        pass  # Never break the banner over an update check

    right_content = "\n".join(right_lines)
    layout_table = Table.grid(padding=(0, 3))
    layout_table.add_column("left", justify="center")
    layout_table.add_column("right", justify="left")
    layout_table.add_row(left_content, right_content)

    version_label = format_banner_version_label()
    release_info = get_latest_release_tag()
    if release_info:
        _tag, _url = release_info
        title_markup = f"[bold {title_color}][link={_url}]{version_label}[/link][/]"
    else:
        title_markup = f"[bold {title_color}]{version_label}[/]"
    outer_panel = Panel(
        layout_table,
        title=title_markup,
        border_style=border_color,
        padding=(0, 2),
    )

    console.print()
    console.print(outer_panel)


def format_banner_version_label() -> str:
    """Return the version label shown in the startup banner title."""
    base = f"Vigil v{VERSION} ({RELEASE_DATE})"
    state = get_git_banner_state()
    if not state:
        return base

    upstream = state["upstream"]
    local = state["local"]
    ahead = int(state.get("ahead") or 0)

    if ahead <= 0 or upstream == local:
        return f"{base} · upstream {upstream}"

    carried_word = "commit" if ahead == 1 else "commits"
    return f"{base} · upstream {upstream} · local {local} (+{ahead} carried {carried_word})"


# =========================================================================
# Non-blocking update check
# =========================================================================

_update_result: Optional[int] = None
_update_check_done = threading.Event()


def prefetch_update_check():
    """Kick off update check in a background daemon thread."""
    def _run():
        global _update_result
        _update_result = check_for_updates()
        _update_check_done.set()
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_update_result(timeout: float = 0.5) -> Optional[int]:
    """Get result of prefetched check. Returns None if not ready."""
    _update_check_done.wait(timeout=timeout)
    return _update_result


# =========================================================================
# Welcome banner
# =========================================================================

def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}M"
        return f"{val:.1f}M"
    elif tokens >= 1_000:
        val = tokens / 1_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}K"
        return f"{val:.1f}K"
    return str(tokens)


def build_welcome_banner(console: "Console", model: str, cwd: str,
                         tools: List[dict] = None,
                         enabled_toolsets: List[str] = None,
                         session_id: str = None,
                         get_toolset_for_tool=None,
                         context_length: int = None,
                         provider: str = None):
    """Build and print the Vigil console header — one language for every profile.

    Args:
        console: Rich Console instance.
        model: Current model name.
        cwd: Current working directory.
        tools: List of tool definitions (used for the non-ops capability line).
        enabled_toolsets: List of enabled toolset names.
        session_id: Session identifier.
        get_toolset_for_tool: Callable to map tool name -> toolset name.
        context_length: Model's context window size in tokens.
        provider: Active provider id. When ``"moa"``, ``model`` is a MoA
            preset name and the banner renders the aggregator instead of a
            bare model slug.
    """
    _render_banner(
        console=console, model=model, cwd=cwd,
        session_id=session_id, context_length=context_length,
        provider=provider, state=_load_banner_state(),
        tools=tools or [], enabled_toolsets=enabled_toolsets or [],
    )
