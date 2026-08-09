#!/usr/bin/env python3
"""Ops profile initializer — legacy entry point.

Thin shim over the packaged ``hermes_cli.ops_init`` module so the
pre-wheel command ``python3 scripts/ops_init.py`` keeps working. New
installations should use ``vigil ops-init`` (console script, same flags):
    vigil ops-init [--root PATH] [--env test|uat|prod] [--force] [--no-alias]
"""

from __future__ import annotations

import sys


def main() -> int:
    from hermes_cli.ops_init import main as _ops_init_main
    return _ops_init_main()


if __name__ == "__main__":
    raise SystemExit(main())
