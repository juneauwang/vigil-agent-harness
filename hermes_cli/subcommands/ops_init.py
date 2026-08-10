"""``vigil ops-init`` subcommand parser.

Attaches the ops profile initializer to the top-level CLI. The handler is
injected (same pattern as every other subcommand) so the parser module stays
light and ``main`` is not imported at build time.
"""

from __future__ import annotations

from typing import Callable


def build_ops_init_parser(subparsers, *, cmd_ops_init: Callable) -> None:
    """Attach the ``ops-init`` subcommand to ``subparsers``."""
    ops_init_parser = subparsers.add_parser(
        "ops-init",
        help="Initialize the ops profile (topology table + sample runbooks)",
        description=(
            "Create the ops profile with the ops config (topo toolset + TOPO "
            "memory provider + permission matrix) and seed the sample topology "
            "table and runbooks into <root>/profiles/ops. Idempotent — "
            "existing files are left untouched unless --force is passed."
        ),
    )
    ops_init_parser.add_argument(
        "--root",
        help="Vigil root (default ~/.vigil or $VIGIL_HOME/$HERMES_HOME); the ops profile "
             "is created at <root>/profiles/ops",
    )
    ops_init_parser.add_argument(
        "--env", choices=("test", "uat", "prod"), default="test",
        help="Initial ops.permissions.env (default test — safe default; "
             "switch to prod after review)",
    )
    ops_init_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing config.yaml / topology.yaml / entities/ "
             "(never touches .env, sessions, or other user data)",
    )
    ops_init_parser.add_argument(
        "--no-alias", action="store_true",
        help="Do not create the ops wrapper command (only vigil -p ops works)",
    )
    ops_init_parser.set_defaults(func=cmd_ops_init)
