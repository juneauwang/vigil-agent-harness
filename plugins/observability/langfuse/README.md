# Langfuse Observability Plugin

This plugin ships bundled with Vigil but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

Pick one:

```bash
# Interactive: walks you through credentials + SDK install + enable
vigil tools  # → Langfuse Observability

# Manual
pip install langfuse
vigil plugins enable observability/langfuse
```

## Required credentials

Set these in `~/.vigil/.env` (or via `vigil tools`):

```bash
VIGIL_LANGFUSE_PUBLIC_KEY=pk-lf-...
VIGIL_LANGFUSE_SECRET_KEY=sk-lf-...
VIGIL_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
vigil plugins list                 # observability/langfuse should show "enabled"
vigil chat -q "hello"              # then check Langfuse for a "Vigil turn" trace
```

## Optional tuning

```bash
VIGIL_LANGFUSE_ENV=production       # environment tag
VIGIL_LANGFUSE_RELEASE=v1.0.0       # release tag
VIGIL_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
VIGIL_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
VIGIL_LANGFUSE_DEBUG=true           # verbose plugin logging
```

## Disable

```bash
vigil plugins disable observability/langfuse
```
