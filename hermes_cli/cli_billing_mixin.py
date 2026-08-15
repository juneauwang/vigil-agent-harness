"""Billing handlers for the interactive CLI (god-file decomposition).

This module hosts the Nous billing methods lifted out of
``cli.py``'s ``HermesCLI`` class. ``HermesCLI`` inherits
``CLIBillingMixin`` so every ``self.<handler>`` call resolves unchanged
via the MRO — behavior-neutral apart from focused billing fixes.

Import discipline mirrors ``hermes_cli.cli_commands_mixin``:
  * Neutral, non-cyclic dependencies are imported at module top level below.
  * cli.py-internal symbols (the ``_cprint``/``_b``/``_d`` helpers and
    display constants) are imported LAZILY inside each method via
    ``from cli import ...``. The mixin never imports ``cli`` at module load
    time, avoiding the cycle created when ``cli.py`` imports this mixin.
"""

from __future__ import annotations



class CLIBillingMixin:
    """Mixin holding interactive-CLI billing handlers."""

    def _print_nous_credits_block(self) -> bool:
        """Print the Nous dollar balance block (two-bar view) when a Nous account
        is logged in. Returns True if it printed anything.

        Prefers the shared dollar usage model (``agent.billing_usage`` — two-bar
        plan/top-up view, dollars-only, the /usage source of
        truth). Falls back to the legacy ``nous_credits_lines`` text only when the
        model is unavailable. Agent-independent (a portal fetch gated on "a Nous
        account is logged in"), so /usage shows the block even in the TUI
        slash-worker subprocess that resumes WITHOUT a live agent. Fail-open and
        wall-clock-bounded; honors VIGIL_DEV_CREDITS_FIXTURE for offline testing.
        """
        from cli import _cprint, _b, _d

        try:
            from agent.billing_usage import build_usage_model, format_renews

            usage = build_usage_model()
        except Exception:
            usage = None
            format_renews = None  # type: ignore

        if usage is not None and usage.available and format_renews is not None:
            printed_any = False
            plan = usage.plan_name or ("Free" if usage.status == "free" else None)
            renews_display = getattr(usage, "renews_display", None) or format_renews(usage.renews_at)
            renews = f" · renews {renews_display}" if renews_display else ""
            if plan:
                print()
                _cprint(f"  {_b(f'Plan: {plan}{renews}')}")
                printed_any = True

            # All lines below go through _cprint (same renderer as the Plan line) so
            # ordering is deterministic: raw print() and _cprint() flush to different
            # buffers under patch_stdout and interleave nondeterministically (the bar
            # would race above/below the Plan line across states). Keep one path.
            for _bar_ln in self._usage_bar_lines(usage, usage.plan_name):
                _cprint(_bar_ln)
                printed_any = True
            if usage.has_topup and usage.total_spendable_usd is not None:
                _cprint(f"  Total spendable: ${usage.total_spendable_usd:,.2f}")

            if usage.status == "free":
                _cprint(f"  {_d('> Free · free models only. Manage billing on the portal.')}")
                printed_any = True
            elif usage.status == "low":
                _amt = f"${usage.total_spendable_usd:,.2f}" if usage.total_spendable_usd is not None else "under $5"
                _low = f"! Low balance · {_amt} left. Manage billing on the portal."
                _cprint(f"  {_low}")
                printed_any = True

            if printed_any:
                return True

        # Fallback: legacy text lines (only when the model is unavailable).
        from agent.account_usage import nous_credits_lines

        lines = nous_credits_lines()
        if not lines:
            return False
        print()
        for line in lines:
            print(f"  {line}")
        return True
    def _usage_bar_lines(self, usage, plan_name) -> list:
        """The plan + top-up dollar bars as ready-to-print lines (filled = remaining).

        Returns [] when there's nothing to draw. The caller resolves ``plan_name``
        (the plan-bar label) and picks its own print fn — block ordering differs
        per surface (``_cprint`` vs ``print`` under patch_stdout). One source of
        truth for the bar format across /usage surfaces.
        """
        lines: list = []
        pb = getattr(usage, "plan_bar", None) if usage else None
        if pb is not None and pb.total_usd > 0:
            filled = max(0, min(10, round(pb.fill_fraction * 10)))
            bar = ("█" * filled) + ("░" * (10 - filled))
            pct_s = f" · {pb.pct_used}% used" if pb.pct_used is not None else ""
            label = (plan_name or "plan").ljust(8)[:8]
            lines.append(f"  {label}[{bar}]  ${pb.remaining_usd:,.2f} left of ${pb.total_usd:,.2f}{pct_s}")
        tb = getattr(usage, "topup_bar", None) if usage else None
        if tb is not None and tb.remaining_usd > 0:
            lines.append(f"  {'top-up'.ljust(8)}[{'█' * 10}]  ${tb.remaining_usd:,.2f} · never expires")
        return lines

