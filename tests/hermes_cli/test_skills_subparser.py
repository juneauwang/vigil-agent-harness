"""Test that skills subparser doesn't conflict (regression test for #898)."""

import argparse


def test_no_duplicate_skills_subparser():
    """Ensure 'skills' subparser is only registered once to avoid Python 3.11+ crash.

    Python 3.11 changed argparse to raise an exception on duplicate subparser
    names instead of silently overwriting (see CPython #94331).

    This test will fail with:
        argparse.ArgumentError: argument command: conflicting subparser: skills

    if the duplicate 'skills' registration is reintroduced.
    """
    # Force fresh import of the module where parser is constructed
    # If there are duplicate 'skills' subparsers, this import will raise
    # argparse.ArgumentError at module load time
    import sys

    import hermes_cli

    # A forced-fresh import replaces TWO bindings: ``sys.modules[...]`` and the
    # ``hermes_cli.main`` package attribute.  Other test files do
    # ``from hermes_cli import main as cli_main`` at import time and patch that
    # object, while ``hermes_cli.update_cmd._m()`` (the lazy accessor used by
    # the update code path) resolves ``from hermes_cli import main`` through
    # the package ATTRIBUTE at call time.  Leaving the throwaway copy behind
    # makes those two disagree, so a later file's ``PROJECT_ROOT`` patch
    # silently misses and ``vigil update`` runs REAL ``git checkout`` against
    # the checkout root.  Restore both bindings so this test cannot strand
    # them.
    original_main = sys.modules.get("hermes_cli.main")

    # Remove cached module if present
    if original_main is not None:
        del sys.modules['hermes_cli.main']

    try:
        import hermes_cli.main  # noqa: F401
    except argparse.ArgumentError as e:
        if "conflicting subparser" in str(e):
            raise AssertionError(
                f"Duplicate subparser detected: {e}. "
                "See issue #898 for details."
            ) from e
        raise
    finally:
        current = sys.modules.get("hermes_cli.main")
        if original_main is not None and current is not original_main:
            sys.modules["hermes_cli.main"] = original_main
            hermes_cli.main = original_main
