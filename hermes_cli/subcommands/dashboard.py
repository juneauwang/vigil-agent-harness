"""``vigil dashboard`` / ``vigil serve`` subcommand parsers.

``dashboard`` is the browser web UI; ``serve`` is the same gateway, headless —
what the desktop app and remote backends run. ``serve`` also skips the web UI
build (``headless_backend=True``): pure JSON-RPC/WS clients never load the SPA.
Both share one handler (``cmd_dashboard`` → ``start_server``). Extracted from
``hermes_cli/main.py:main()`` (god-file Phase 2); handler injected to avoid
importing ``main``.
"""

from __future__ import annotations

import argparse
from typing import Callable


def _add_server_runtime_args(parser) -> None:
    """Attach the runtime flags shared by ``dashboard`` and ``serve``.

    Both subcommands boot the *same* ``web_server.start_server`` (the
    JSON-RPC/WebSocket gateway). ``dashboard`` opens a browser UI on top of
    it; ``serve`` is the headless backend the desktop app and remote clients
    connect to. The shared server logic lives in one place — only the
    browser-opening behavior and help framing differ.
    """
    parser.add_argument(
        "--port", type=int, default=9119, help="Port (default 9119, 0 for auto-assign by OS)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host (default 127.0.0.1)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "DEPRECATED / NO-OP. Formerly bypassed auth on a non-loopback "
            "bind. As of the June 2026 hardening it no longer disables "
            "authentication — a public bind always requires an auth provider "
            "(password or OAuth). Bind 127.0.0.1 + tunnel to keep it local."
        ),
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help=(
            "Skip the web UI build step and serve the existing dist directly. "
            "Useful for non-interactive contexts (Windows Scheduled Tasks, CI) "
            "where npm may not be available. Pre-build with: cd web && npm run build"
        ),
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help=(
            "When launched from a named profile, run a dedicated server scoped "
            "to that profile instead of routing to the machine-level server. "
            "Default behavior is unified: profile launches attach to (or start) "
            "ONE machine-level server and preselect the profile."
        ),
    )
    # Internal flag set by the unified-launch re-exec (cmd_dashboard) to
    # preselect the launching profile in the SPA switcher. Hidden from --help.
    parser.add_argument(
        "--open-profile",
        dest="open_profile",
        default="",
        help=argparse.SUPPRESS,
    )
    # Lifecycle flags — mutually exclusive with each other and with the
    # start-a-server flags above (if both are passed, --stop / --status win
    # because they exit before the server is started).  The server has no
    # service manager and no PID file, so these scan the process table for
    # `vigil dashboard` / `vigil serve` cmdlines and SIGTERM them directly —
    # the same path `vigil update` uses to clean up stale servers.
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop all running Vigil web server processes and exit",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="List running Vigil web server processes and exit",
    )


def build_dashboard_parser(
    subparsers, *, cmd_dashboard: Callable, cmd_dashboard_register: Callable,
    cmd_dashboard_install: Callable = None, cmd_dashboard_uninstall: Callable = None,
    cmd_dashboard_status: Callable = None,
) -> None:
    """Attach the ``dashboard`` and ``serve`` subcommands.

    Both share the same backend (``cmd_dashboard`` → ``start_server``).
    ``dashboard`` is the browser UI; ``serve`` is the headless backend used by
    the desktop app and remote clients. They are independent surfaces — neither
    "launches" the other — so the desktop app spawns ``serve``, never
    ``dashboard``.
    """
    # =========================================================================
    # dashboard command — the browser web UI
    # =========================================================================
    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Start the web UI dashboard",
        description="Launch the Vigil Agent web dashboard for managing config, API keys, and sessions",
    )
    _add_server_runtime_args(dashboard_parser)
    dashboard_parser.add_argument(
        "--no-open", action="store_true", help="Don't open browser automatically"
    )
    # Backward-compat shim: older upstream desktop app shells (<= 0.15.x) spawn the
    # backend as `vigil dashboard --no-open --tui --host ... --port ...`. The
    # `--tui` flag was removed from this subcommand in cae6b5486 (embedded chat is
    # always on now). When a user's CLI updates past that commit but their desktop
    # app binary has not, argparse used to hard-error with "unrecognized arguments:
    # --tui" and exit(2) — the backend died before becoming ready and the GUI just
    # showed "Vigil couldn't start" with no actionable cause. Accept and silently
    # ignore the flag so an old app + new CLI degrades gracefully instead of
    # bricking. Hidden from --help; safe to delete once the floor app version is
    # well past 0.16.0.
    dashboard_parser.add_argument(
        "--tui",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    # =========================================================================
    # serve command — the headless backend server
    #
    # `serve` boots the exact same gateway as `dashboard` but never opens a
    # browser. It exists so the Vigil Desktop app (and headless remote
    # backends) can launch a backend WITHOUT invoking `dashboard`: the desktop
    # app and the web dashboard are independent surfaces that merely share this
    # server, and neither should appear to launch the other.
    # =========================================================================
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the Vigil backend server (headless; powers the desktop app and remote backends)",
        description=(
            "Run the Vigil backend server — the JSON-RPC/WebSocket gateway the "
            "desktop app and remote clients connect to. Headless: it never opens "
            "a browser UI."
        ),
    )
    _add_server_runtime_args(serve_parser)
    # Accepted but redundant: `serve` is always headless (see set_defaults
    # below). Kept so callers that pass the legacy `--no-open` flag (e.g. the
    # desktop backend spawn) don't trip "unrecognized arguments".
    serve_parser.add_argument(
        "--no-open", action="store_true", help=argparse.SUPPRESS
    )
    serve_parser.add_argument(
        "--ssh-session-token-file",
        dest="ssh_session_token_file",
        metavar="PATH",
        default=None,
        help="Read a one-shot Desktop SSH session token from PATH",
    )
    serve_parser.add_argument(
        "--ssh-owner-nonce",
        dest="ssh_owner_nonce",
        metavar="NONCE",
        default=None,
        help="Identify a Desktop-owned SSH backend process",
    )
    # `headless_backend` marks the lean path: desktop/remote clients speak pure
    # JSON-RPC/WS, so `serve` skips the web UI build AND never serves the SPA
    # (cmd_dashboard exports VIGIL_SERVE_HEADLESS=1). `dashboard` leaves it
    # unset and serves the browser UI as before.
    serve_parser.set_defaults(func=cmd_dashboard, no_open=True, headless_backend=True)

    # `vigil dashboard register` — register a self-hosted dashboard OAuth
    # client with Nous Portal and write the client_id into ~/.vigil/.env.
    # Nested subparser so bare `vigil dashboard` keeps launching the server
    # (set_defaults(func=cmd_dashboard) above remains the default).
    dashboard_subparsers = dashboard_parser.add_subparsers(
        dest="dashboard_subcommand"
    )
    dashboard_register_parser = dashboard_subparsers.add_parser(
        "register",
        help="Register a self-hosted dashboard with Nous Portal (writes the OAuth client ID to .env)",
        description=(
            "Register this install as a self-hosted dashboard with your Nous "
            "Portal account. Creates an OAuth client, writes "
            "VIGIL_DASHBOARD_OAUTH_CLIENT_ID into ~/.vigil/.env, and prints "
            "how to engage the login gate. Requires being logged in (vigil setup)."
        ),
    )
    dashboard_register_parser.add_argument(
        "--name",
        default=None,
        help="Human-readable label for the dashboard (default: an auto-generated name)",
    )
    dashboard_register_parser.add_argument(
        "--redirect-uri",
        dest="redirect_uri",
        default=None,
        help=(
            "Optional public HTTPS OAuth redirect URI for the dashboard, e.g. "
            "https://hermes.example.com/auth/callback. Omit for localhost-only use."
        ),
    )
    dashboard_register_parser.add_argument(
        "--portal-url",
        dest="portal_url",
        default=None,
        help=(
            "Override the Nous Portal base URL for registration (default: the "
            "portal you logged into). The access token must be valid at this "
            "portal. Also settable via VIGIL_DASHBOARD_PORTAL_URL. Mainly for "
            "testing against a staging/preview portal."
        ),
    )
    dashboard_register_parser.set_defaults(func=cmd_dashboard_register)

    # -----------------------------------------------------------------
    # `vigil dashboard install / uninstall / status` — systemd 常驻
    # 产品化（批三十六）：自动创建/卸载/查看常驻 dashboard unit，中间
    # 市场用户不再手写 unit。install 覆盖旧 unit 行为明确（先停再写）。
    # -----------------------------------------------------------------
    if cmd_dashboard_install is not None:
        install_parser = dashboard_subparsers.add_parser(
            "install",
            help="Install the dashboard as a systemd user service (auto-start on login)",
            description=(
                "Create/overwrite the vigil-dashboard.service user unit (systemd "
                "user session required), enable auto-start, and launch the "
                "dashboard. Repeated installs overwrite the unit (old service is "
                "stopped first); --port changes take effect immediately."
            ),
        )
        install_parser.add_argument(
            "--port", type=int, default=9119,
            help="Port the unit should serve on (default 9119)",
        )
        install_parser.set_defaults(func=cmd_dashboard_install)
    if cmd_dashboard_uninstall is not None:
        uninstall_parser = dashboard_subparsers.add_parser(
            "uninstall",
            help="Remove the systemd dashboard service and stop it (idempotent)",
            description=(
                "Stop + disable + delete vigil-dashboard.service. Idempotent: "
                "succeeds even when the unit does not exist."
            ),
        )
        uninstall_parser.set_defaults(func=cmd_dashboard_uninstall)
    if cmd_dashboard_status is not None:
        status_parser = dashboard_subparsers.add_parser(
            "status",
            help="Show the systemd dashboard service status (active/exited + URL)",
            description=(
                "Summarize the vigil-dashboard.service state: active/exited, "
                "auto-start enabled, and the access URL/port."
            ),
        )
        status_parser.set_defaults(func=cmd_dashboard_status)
