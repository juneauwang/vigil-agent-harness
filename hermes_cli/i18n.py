"""Message-catalog i18n for the Vigil ops console (CLI + dashboard backend).

Phase 1 of backend/CLI message i18n (zh-baseline product positioning:
私有化/中文, with an en switch).

Scope split (deliberate, see report):
- ``agent/i18n`` — the AGENT core's static strings (approval prompts, gateway
  slash replies). 17-locale YAML catalogs under ``locales/``, en-baseline,
  ``display.language`` knob. Untouched by this module.
- ``hermes_cli.i18n`` (this module) — ops-console messages (CLI subcommands,
  tools, dashboard backend payloads). zh + en only, zh-default.

Resolution order for the ops-console language:

1. Explicit ``lang=`` argument passed to :func:`t`
2. ``VIGIL_LANG`` environment variable
3. ``ops.lang`` from config.yaml
4. ``"zh"`` (default — existing behavior for zh users is unchanged)

Migration safety: call sites pass the ORIGINAL Chinese text as
``zh_default`` — if a key is missing from the en catalog (or i18n is bypassed
entirely), the user sees exactly the pre-i18n Chinese string, never a bare
key and never an accidental English swap::

    from hermes_cli.i18n import t
    print(t("runbook.not_found", "未找到 Runbook：{name}", name=name))

The en catalog (``_EN``) holds the English entry for every converted key;
keys are semantic English slugs grouped by surface. ``{placeholder}``
interpolation uses ``str.format``; on a format error the raw string is
returned (a broken message must never crash a tool).
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LANG = "zh"

_EN_ALIASES = {"en", "en-us", "en-gb", "english"}
_ZH_ALIASES = {"zh", "zh-cn", "zh-hans", "zh-sg", "chinese", "mandarin", ""}

# ---------------------------------------------------------------------------
# English catalog — one entry per converted message. zh text lives at the
# call site (the zh_default argument) so migration cannot drift the Chinese.
# ---------------------------------------------------------------------------

_EN: dict[str, str] = {
    # ── vigil approvals (self-evolving allowlist CLI, batch86) ──────────────
    "approvals_mem.read_failed": "✗ Failed to read approval_memory: {exc}",
    "approvals_mem.empty": "(empty: approval_memory.yaml missing or has no entries — only built-in seeds are active at runtime)",
    "approvals_mem.col_template": "Template",
    "approvals_mem.col_status": "Status",
    "approvals_mem.col_count": "N",
    "approvals_mem.col_skip": "Skip",
    "approvals_mem.col_approved_at": "Approved at",
    "approvals_mem.col_source": "Source",
    "approvals_mem.list_note": "Legend: active = sedimented, approval skipped next time; banned = denied by user, never auto-sedimented (only forget clears it); seeds default active (source=seed), file entries override.",
    "approvals_mem.forget_requires_template": "✗ A command template is required (e.g.: vigil approvals forget lscpu)",
    "approvals_mem.forget_failed": "✗ Forget failed: {exc}",
    "approvals_mem.forget_not_found": "✗ Template {template!r} is not in the allowlist (neither seed nor sedimented); nothing to forget.",
    "approvals_mem.forgot": "✓ Forgot {template!r} (set inactive, count reset): the command returns to prompting for approval. Approve 3 more times (or choose 'never ask again') to re-sediment. Audited.",
    "approvals_mem.export_description": "Command-level self-evolving allowlist v0.1 (approval_memory.yaml) — user-approved read-only command templates sediment to active and skip approval next time; seeds are built-in read-only seeds (cold-start starting point, active by default, not stored in the file).",
    "approvals_mem.status_active": "active (sedimented, approval skipped next time)",
    "approvals_mem.status_pending": "pending (counting)",
    "approvals_mem.status_banned": "banned (denied before, never auto-sedimented)",
    "approvals_mem.status_inactive": "inactive (revoked, prompts again)",
    "approvals_mem.kind_seed": "seed (built-in read-only seed)",
    "approvals_mem.kind_evolved": "self-evolved/managed",

    # ── vigil approvals parser help ─────────────────────────────────────────
    "approvals.cmd_help": "Approval tools: suggest sedimentation / self-evolving allowlist view-forget-export",
    "approvals.cmd_desc": "Approval-related tools. `vigil approvals suggest` mines the session store for past approval decisions and proposes config.yaml command_allowlist entries; `vigil approvals list / forget / export` manage the command-level self-evolving allowlist (approval_memory.yaml, v0.1 — usage-history-driven permission evolution: user-approved read-only command templates sediment to active and skip approval next time).",
    "approvals.list_help": "List the command-level self-evolving allowlist (seeds + self-evolved, all fields)",
    "approvals.list_desc": "List the full approval_memory.yaml command-level self-evolving allowlist: template / status (active=sedimented, skips approval next time; pending=counting; banned=denied before, never auto-sedimented; inactive=revoked) / success count / skip / approved-at / source task; built-in read-only seeds (lscpu/free/cat/..., cold-start starting point) are tagged separately.",
    "approvals.list_json_help": "Output as JSON (machine-readable)",
    "approvals.forget_help": "Revoke one self-evolving allowlist entry (the command returns to prompting for approval)",
    "approvals.forget_desc": "Revoke one command-level allowlist entry (set inactive, reset count): the command returns to prompting for approval; approve 3 more times to re-sediment. Seed entries can be revoked (after `forget lscpu`, lscpu prompts again); banned entries (denied by the user) can only be lifted explicitly via this command — they never revive automatically. Use normalized template names (lscpu / cat / lsblk).",
    "approvals.forget_template_help": "Command template to revoke (normalized name, e.g. lscpu / cat / lsblk)",
    "approvals.export_help": "Export the self-evolving allowlist (stdout YAML; --json for JSON)",
    "approvals.export_desc": "Export the full approval_memory.yaml to stdout (YAML by default, --json for JSON): entries include template/status/count/never_denied/user_opt_in/approved-at/source task, plus the built-in seed list — for user backup/review (open-source accountability: viewable/revocable/auditable).",
    "approvals.export_json_help": "Output as JSON",

    # ── vigil alerts CLI (batch87) ──────────────────────────────────────────
    "alerts.no_match": "no matching runbook",
    "alerts.hint_parens": "({hint})",
    "alerts.conf_high": "high confidence",
    "alerts.conf_medium": "medium confidence",
    "alerts.basis_trigger": "trigger-word hit",
    "alerts.basis_fuzzy": "fuzzy match",
    "alerts.match_head": "→ {name} ({label} · {basis}",
    "alerts.match_keyword": ' "{keyword}"',
    "alerts.match_close": ")",
    "alerts.match_title": " · {title}",
    "alerts.alternatives": "; runner-ups: {names}",
    "alerts.list_header": "Active alerts: {count} ({matched} with runbook suggestions)",
    "alerts.alert_line": "- [{sev}] {alertname}",
    "alerts.unknown_alert": "unknown alert",
    "alerts.disposition_label": "\n  Suggested disposition: ",
    "alerts.list_footer": "\nNote: the above are disposition suggestions only (never auto-executed). To execute, confirm via runbook_execute (agent) / the web monitoring page / the existing execution chain (matrix + approval gates).",
    "alerts.upstream_error": "✗ Alertmanager upstream error: {exc}",
    "alerts.history_header": "Recent triage audit (runtime/alert_triage.jsonl): {n} entries",
    "alerts.history_row": "- {ts} · {total} alerts ({matched} matched)",
    "alerts.history_conf": " ({confidence}/{matched_by})",

    # ── interactive CLI arg errors ──────────────────────────────────────────
    "cli.arg_missing_value": "  ✗ --{name} missing a value",
    "cli.arg_unknown": "  ✗ unknown option --{name}",

    # ── sudo_exec ────────────────────────────────────────────────────────────
    "sudo.rejected_reason": "sudo_exec rejected command: {reason} (fail-closed; read-only diagnostics must be a single command)",
    "sudo.reason.shell_nested_c": "nested shell (bash -c …)",
    "sudo.reason.shell_first_token": "nested shell (first token is a shell: sh -c … / bash script.sh / interactive shell)",
    "sudo.reason.shell_pipe": "nested shell (pipe into a shell: curl … | sh)",
    "sudo.reason.heredoc": "heredoc (multi-command script shape)",
    "sudo.reason.redirect": "redirection (> / >> / 2>&1 / >&)",
    "sudo.reason.background": "background execution (&) — chain steps with && instead, or write a script file and sudo_exec it",
    "sudo.reason.multi_command": "multi-command separator (;)",
    "sudo.reason.logical_or": "logical OR (||)",
    "sudo.reason.cmd_subst": "command substitution ($(…))",
    "sudo.reason.backtick": "command substitution (backticks)",
    "sudo.reason.nested_sudo": "embedded sudo (this tool already provides privilege escalation)",
    "sudo.reason.multiline": "multi-line command",
    "sudo.unparseable": "sudo_exec rejected command: unparseable ({exc})",
    "sudo.empty_command": "sudo_exec rejected command: empty command",
    "sudo.clarify_question": "sudo_exec needs the sudo password for {host}: please enter it (stored securely in the credential vault 0600, referenced by name only, never in the session plaintext)",
    "sudo.local_host": "this host",
    "sudo.host_unspecified": "(unspecified)",
    "sudo.local_needs_secret_cred": "Local sudo requires a secret/askpass credential (ssh_key carries no password plaintext) — add a credential declaration to the topology table or run the command manually",
    "sudo.askpass_missing": "askpass script not found: {path}",
    "sudo.remote_needs_password": "Remote sudo needs a password (credential is an ssh_key for {user} but sudo still demands authentication) — configure a secret credential (declare secret in the topology host credential, carrying the sudo password) or run manually; never guess passwords or trawl ~/.ssh/ (§Q/§AD lesson)",
    "sudo.remote_needs_secret_cred": "Remote sudo requires a secret credential (carrying the sudo password) or an ssh_key (passwordless root sudo direct) — askpass is not supported for remote injection yet; add a secret credential declaration to the topology table or run manually",
    "sudo.cred_not_in_vault": "Credential not found in the vault: {ref}",
    "sudo.scp_failed": "scp upload failed: {detail}",
    "sudo.unknown_error": "unknown error",
    "sudo.approval_not_granted": "Approval not granted (fail-closed, not executing)",
    "sudo.requires_command": "sudo_exec requires command (the read-only diagnostic command to run via sudo locally/remotely)",
    "sudo.matrix_denied": "Permission matrix denied execution: {description}",
    "sudo.missing_cred": "sudo_exec missing sudo credential: topology host {host} has no credential reference. Stop auto-retrying: 1) run the command manually 2) or add a credential declaration to the topology table (secret type, see vssh / topo credential) 3) or ask the user for correct credentials; never trawl ~/.ssh/ for keys, guess secret fields, or retry with different usernames (§Q/§AD lesson — triggers rate limiting)",
    "sudo.vault_store_failed": "sudo_exec could not store the credential in the vault: {exc}",
    "sudo.timeout": "sudo_exec timed out ({n}s): sudo {command}",
    "sudo.cannot_execute": "sudo_exec could not execute: {exc}",
    "sudo.failed": "sudo_exec failed: {exc}",
    "sudo.auth_failed": "sudo authentication failed (wrong or ineffective credential) — stop auto-retrying: 1) run the command manually 2) fix/add the topology credential declaration 3) or ask the user for the correct password; never keep guessing secret fields or retry with different usernames (§Q/§AD lesson — triggers rate limiting)",
    "sudo.auth_failed_collected": " (the user-provided password was stored in the vault as {vault}; if it is wrong, ask the user to update that credential or run credential_vault expire)",
    "sudo.cred_collected_warning": "Executed with vault credential {vault}; consider adding a credential declaration to the topology table's {host} row (type: secret, ref: {vault}) so future runs resolve it automatically",

    # ── ops permission matrix descriptions ─────────────────────────────────
    "opsperm.required": "⚠ Ops matrix forces human confirmation ({action} × {env} = {{approve: required}}, overriding approvals.mode)",
    "opsperm.prod_hardgate": "⚠ prod change forces human confirmation ({action} × {env}): bare terminal commands must not bypass runbook-executor matrix adjudication (OPS-DELTA #95)",
    "opsperm.unknown": "⚠ Could not recognize command intent (unknown): {command} is not in the command-classification rule table, matrix coverage gap → handled conservatively (default approve goes through the approval gate); register a new rule in OPS-DELTA.",
    "opsperm.approve": "Ops matrix adjudicated {action} × {env} as requiring approval (approve)",
    "opsperm.rule_suffix": "{desc} (rule {rule})",
    "opsperm.note_suffix": "{desc}; {note}",
    "opsperm.chain_suffix": "{desc} (chained commands take the conservative branch: {joined})",

    # ── runbook executor ─────────────────────────────────────────────────────
    "runbook_exec.truncated": "\n…(truncated, {n} chars removed)",
    "runbook_exec.target_required": "target is required (a topology entity reference)",
    "runbook_exec.varref_bad_trigger": "{where} variable reference {{{path}}} is invalid — trigger_context references look like {{{{ trigger_context.alertname }}}}",
    "runbook_exec.varref_field_missing": "{where} variable reference {{{path}}}: field {key!r} was not injected into the trigger context (available: {available}) — the trigger reason is injected by the scheduler/caller",
    "runbook_exec.varref_bad_steps": "{where} variable reference {{{path}}} is invalid — must be {{{{ steps.<id>.params.<key> }}}} / {{{{ steps.<id>.outputs.<key> }}}}",
    "runbook_exec.varref_step_missing": "{where} variable reference {{{path}}} points to step {ref_id!r} — that step has not executed yet or does not exist (steps run in order; only executed steps can be referenced)",
    "runbook_exec.varref_key_missing": "{where} variable reference {{{path}}}: {key!r} does not exist in step {ref_id!r}'s {seg}",
    "runbook_exec.varref_not_scalar": "{where} variable reference {{{path}}} must reference a scalar value, got {type}",
    "runbook_exec.varref_invalid": "{where} variable reference {{{path}}} is invalid",
    "runbook_exec.matrix_missing": "Permission matrix not initialized (matrix.yaml missing); runbook execution denied — the matrix is the prerequisite for permission adjudication (§11.7 safety asset); missing = permission system unavailable (fail-closed). Fix: run vigil setup (or vigil ops-init) to generate the matrix first.",
    "runbook_exec.matrix_level_head": "Ops matrix {action}@{env} level",
    "runbook_exec.matrix_level_required": "=forced manual ({{approve: required}}, no smart, no allowlist)",
    "runbook_exec.matrix_level_execute": "=execute (rollback step forces human confirmation override)",
    "runbook_exec.matrix_level_approve": "=approve (interactive approval)",
    "runbook_exec.approval_not_passed": "Approval not passed ({action}@{env}) — fail-closed, not executing",
    "runbook_exec.not_asset_approved": "runbook has not passed asset approval (missing approved_at/approved_by pre-approval markers) — scheduled-execution exemption presumes human review at creation time; re-create via runbook_create first (asset approval writes the markers)",
    "runbook_exec.hash_drift": "runbook content changed after approval (approved_version={version}, current hash={current}) — execution exemption void; re-run asset approval via runbook_create before scheduled execution",
    "runbook_exec.local_timeout": "Execution timed out ({timeout}s)",
    "runbook_exec.local_failed": "Execution failed: {exc}",
    "runbook_exec.remote_cred_failed": "Remote credential resolution failed: {exc}",
    "runbook_exec.remote_timeout": "Remote execution timed out ({timeout}s)",
    "runbook_exec.remote_failed": "Remote execution failed: {exc}",
    "runbook_exec.remote_sudo_needs_cred": "Remote sudo requires a topology credential (secret) reference for {host}",
    "runbook_exec.local_sudo_needs_cred": "Local sudo requires a topology credential (secret/askpass) reference — add a credential declaration for privileged commands or have the user run them manually",
    "runbook_exec.sudo_timeout": "sudo execution timed out ({timeout}s)",
    "runbook_exec.sudo_failed": "sudo execution failed: {exc}",
    "runbook_exec.transfer_needs_paths": "transfer_file requires source.path and dest.path",
    "runbook_exec.scp_argv_failed": "scp argument construction failed: {exc}",
    "runbook_exec.scp_timeout": "scp timed out ({timeout}s)",
    "runbook_exec.scp_failed": "scp execution failed: {exc}",
    "runbook_exec.asset_missing": "Script asset {name!r} does not exist ({dir}) — create it with script_asset_create (content passes the tirith scan + asset approval writes the pre-approval markers); run_script only references assets, never inline scripts",
    "runbook_exec.asset_no_marker": "Script asset {name!r} has no approval marker ({meta} missing) — assets must pass asset approval via script_asset_create before run_script can reference them",
    "runbook_exec.asset_marker_corrupt": "Script asset {name!r} approval marker corrupted: {exc}",
    "runbook_exec.asset_not_approved": "Script asset {name!r} lacks pre-approval markers (approved_at/approved_by) — re-run asset approval",
    "runbook_exec.asset_hash_drift": "Script asset {name!r} content changed after approval (hash drift) — execution exemption void; re-run asset approval (script_asset_create --force)",
    "runbook_exec.script_timeout": "Script execution timed out ({timeout}s)",
    "runbook_exec.script_failed": "Script execution failed: {exc}",
    "runbook_exec.topology_missing": "{ctx} topology table missing — target resolution requires the topology (confirm the entity via topo_query)",
    "runbook_exec.rollback_desc": "⚠ Rollback (rollback scenario {scene}): {desc}",
    "runbook_exec.scene_default": "(default)",
    "runbook_exec.command_failed": "Command failed (exit {code}): {desc}\n{detail}",
    "runbook_exec.expect_attempt_ok": "Attempt {attempt}/{attempts} passed: {detail}",
    "runbook_exec.expect_failed": "expect not satisfied ({attempts} attempts all failed, last: {detail})",
    "runbook_exec.step_exception": "Execution exception: {exc}",
    "runbook_exec.on_failure_invalid": "on_failure invalid: {value!r}",
    "runbook_exec.rollback_scene_missing": "rollback scenario {name!r} does not exist (available: {available})",
    "runbook_exec.no_rollback_scene": "runbook defines no rollback scenario; cannot roll back",
    "runbook_exec.rollback_step_failed": "rollback step {id!r} failed: {error} — rollback failure → forced stop, human intervention required",

    # ── runbook tools (load/checkpoint/create common paths; Phase-1 subset) ──
    "runbook_tools.env_mismatch": "runbook environment {rb_env} differs from the current session environment {session_env}; cross-environment operations are adjudicated per command by the command-level permission matrix (L2 and above go through approval), not rejected wholesale.",
    "runbook_tools.note_v2": "v0.2 runbook (declarative actions, no commands): to execute, call the runbook_execute tool — the executor generates commands from action × target type × managed_by, passes the matrix approval gate, and checks expect (change actions poll-wait for readiness by default). This tool only returns the spec; do not replay the steps via terminal yourself.",
    "runbook_tools.note_v1": "This tool executes nothing; step commands are executed by the agent via the terminal, each passing the permission matrix.",
    "runbook_tools.note_vault_refs": " This runbook contains {n} <secret:...> credential references: plaintext is read from the vault/secret only at execution time and injected into the command immediately (the command string passes redact); never interpolate plaintext into the conversation or command text.",
    "runbook_tools.note_improve": " If this execution found improvements (new pitfalls/new commands), propose updating the runbook to the user (runbook_create overwrite=true).",
    "runbook_tools.data_missing": "runbooks data missing ({dir}) — run vigil topo-discover first or create runbooks/*.yaml manually.",
    "runbook_tools.not_found": "runbook not found: {name} (omit the argument to list all)",
    "runbook_tools.validation_failed": "runbook validation failed: {exc}",
    "runbook_tools.no_match": 'No runbook matches "{query}". Available runbooks: ',
    "runbook_tools.not_found_plain": "runbook not found: {name}",
    "runbook_tools.checkpoint_is_v2": "runbook {name} is schema v0.2 (declarative actions, no commands): checkpoint is the v0.1 checklist stage gate; for v0.2 use runbook_execute (executor generates commands + matrix approval gate + expect/on_failure + execution records).",
    "runbook_tools.not_checklist": "runbook {name} is not a checklist runbook (kind=deploy, checklist=true); no checkpoint needed",
    "runbook_tools.bad_status": "status must be pass/fail, got {status!r}",
    "runbook_tools.no_such_step": "runbook {name} has no step {step_id!r} (available: {ids})",
    "runbook_tools.gate_blocked": "Stage gate blocked advancement: step {step_id!r} has earlier steps not all passed ({blocked}). Complete the earlier checks first (runbook_load to view steps), then runbook_checkpoint(..., status='pass') to advance.",
    "runbook_tools.gate_recorded": "Stage-gate state recorded (for audit).",
    "runbook_tools.asset_matrix_missing": "Permission matrix not initialized (matrix.yaml missing); runbook asset approval denied — the matrix is the prerequisite for permission adjudication (§11.7 safety asset); missing = permission system unavailable (fail-closed). Fix: run vigil setup (or vigil ops-init) to generate the matrix before writing.",
    "runbook_tools.asset_approval_desc": "runbook {name} (env={env}) content approval — actions: {actions} (matrix verdict: {verdict})",
    "runbook_tools.asset_verdict_manual": "contains forced-manual high-risk actions",
    "runbook_tools.asset_verdict_normal": "regular levels",
    "runbook_tools.asset_approval_failed": "Asset approval not passed",

    # ── approval registry (web approval center) + prompt extras ─────────────
    "approval.learn_hint": "      (l)earn — stop asking for this class of read-only commands (writes to the command-level self-evolving allowlist v0.1)",
    "approval.not_found": "Approval not found: {id}",
    "approval.timeout_approve": "Approval timed out (wait policy: no auto-approval, command stays pending)",
    "approval.timeout_deny": "Approval timed out (wait policy: no auto-denial, command stays pending)",
    "approval.bad_scope": "scope must be once/session/permanent",
    "approval.scope_session_unsupported": "This approval does not support the session scope (the prod change confirmation gate only allows once)",
    "approval.scope_permanent_unsupported": "This approval does not support the permanent scope",
    "approval.sudo_stdin_blocked": "Provide the password in the conversation (this use only; the system stores it securely and never echoes it), or write SUDO_PASSWORD to .env; otherwise run the sudo command manually in your own terminal. Never pipe guessed passwords into 'sudo -S' — that is a brute-force vector.",

    # ── monitoring (frontend matchers depend on both languages) ─────────────
    "monitoring.prom_unavailable": "Prometheus not configured (config ops.prometheus.endpoint); only health probing is available.",
    "monitoring.alertmanager_unavailable": "Alertmanager not configured (config ops.prometheus.alertmanager); only health probing is available.",

    # ── interactive session: slash help / boot hints / usage panel ─────────
    "slash.env_help": "View/switch the session ops environment (from the defined ops.environments list; /env with no argument shows the current environment and the available list)",
    "slash.topo_help": "In-session topology discovery — runs the discovery engine locally (/topo [host...] [--env prod] [--user root] [--key path]; no args = interactive collection). Note: --sudo-password is not supported in-session (credential discipline: passwords never enter LLM session arguments); root-only probes need `vigil topo-discover --sudo-password` at the CLI layer or a pre-provisioned topology credential",
    "boot.loadingConfig": "· Loading config and models…",
    "boot.ready": "· Startup ready",
    "boot.loadingPlugins": "· Loading plugins and tools…",
    "boot.pluginsLoaded": "· Plugins and tools loaded",
    "usage.costIncluded": " · cost included (subscription)",
    "usage.estCostApprox": " · est. cost ≈${amount}",
    "usage.estCostLabeled": " · est. cost {label}",
    "usage.sessionSummary": "Tokens:         📊 This session: input {inp} · output {out} · cache {cache} · reasoning {reasoning} · total {total} tokens",
    "usage.cacheHitRate": " · cache hit rate {rate}%",

    # ── matrix data-layer warnings (coverage dashboard) ─────────────────────
    "matrix.missing_actions": "matrix.{env} is missing {n} actions ({shown}) — defaulting to approve (conservative).",
    "matrix.source_unknown": "sources.{env}.{act}: unknown source {source!r}; kept as-is.",

    # ── vigil ops-init wizard ────────────────────────────────────────────────
    "opsinit.env_mapped": "· Note: custom environment {env} mapped to the {tier} tier (the environment enum is now {tiers}; permissions never loosen, uat→prod only gets stricter). Adjust in config.yaml ops.environments if needed",
    "opsinit.skip_exists": "· {name} already exists, skipping (use --force to overwrite): {path}",
    "opsinit.wrote": "· Wrote {name}: {path}",
    "opsinit.missing_sample": "Missing sample topology {src} — initialization aborted.",
    "opsinit.missing_sample_dir": "Missing sample directory {src} — initialization aborted.",
    "opsinit.skip_dir_nonempty": "· {name} already exists and is non-empty, skipping (use --force to overwrite): {path}",
    "opsinit.wrote_entities": "· Wrote entities/: {n} entity profiles → {path}",
    "opsinit.wrote_services": "· Wrote services/: {n} service directories → {path}",
    "opsinit.wrote_hardware": "· Wrote hardware/: {n} hardware profiles → {path}",
    "opsinit.wrote_runbooks": "· Wrote runbooks/: {n} runbooks → {path}",
}


def _normalize_lang(value: Any) -> str:
    """Map a user-supplied language value to ``"zh"`` or ``"en"``.

    Unknown / empty values fail safe to ``"zh"`` (the pre-i18n behavior).
    """
    if not isinstance(value, str):
        return DEFAULT_LANG
    key = value.strip().lower()
    if key in _ZH_ALIASES:
        return "zh"
    if key in _EN_ALIASES:
        return "en"
    base = key.split("-", 1)[0]
    if base == "en":
        return "en"
    if base == "zh":
        return "zh"
    return DEFAULT_LANG


@lru_cache(maxsize=1)
def _config_lang() -> str:
    """Read ``ops.lang`` from config.yaml once per process ("" = unset)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        return str((cfg.get("ops") or {}).get("lang") or "")
    except Exception as exc:  # config unreadable → zh default
        logger.debug("ops i18n: could not read ops.lang from config: %s", exc)
        return ""


def reset_language_cache() -> None:
    """Drop the cached ``ops.lang`` lookup (call after saving config)."""
    _config_lang.cache_clear()


def get_lang(lang: str | None = None) -> str:
    """Resolve the active ops-console language: arg > env > config > zh."""
    if lang:
        return _normalize_lang(lang)
    env = os.environ.get("VIGIL_LANG")
    if env:
        return _normalize_lang(env)
    cfg = _normalize_lang(_config_lang())
    if cfg != DEFAULT_LANG:
        return cfg
    return DEFAULT_LANG


def t(key: str, zh_default: str, lang: str | None = None, **format_kwargs: Any) -> str:
    """Translate an ops-console message key.

    Parameters
    ----------
    key
        Dotted catalog slug, e.g. ``"runbook.not_found"``.
    zh_default
        The ORIGINAL Chinese text (with ``{placeholder}`` tokens where the
        message interpolates). Returned whenever English is not active or the
        key is missing — migration can never change what zh users see.
    lang
        Explicit language override (tests / one-off calls).
    **format_kwargs
        ``str.format`` substitutions applied to whichever text is chosen.
    """
    target = get_lang(lang)
    text = zh_default
    if target == "en":
        en = _EN.get(key)
        if en is not None:
            text = en
        # Missing en entry → keep the original Chinese (safe migration).
    # Always run str.format: brace-doubled literals ({{...}}) must collapse
    # even when a message takes no arguments. A stray single { raises and
    # falls back to the raw text (never crashes, never mangles).
    try:
        return text.format(**format_kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "ops i18n format failed for key=%r: %s", key, exc,
        )
        return text


def register_en(entries: dict[str, str]) -> None:
    """Merge additional English entries (used by tests / optional plugins)."""
    _EN.update(entries)


__all__ = ["DEFAULT_LANG", "t", "get_lang", "reset_language_cache", "register_en"]
