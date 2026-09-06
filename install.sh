#!/usr/bin/env bash
# Vigil — one-line installer (Linux / macOS)
#
#   curl -fsSL https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/v1.0/install.sh | bash
#
# What this does (read before you pipe):
#   1. Detects python3 (>= 3.10) — Vigil is a Python package, nothing to dodge here.
#   2. Creates an isolated venv at ~/.local/share/vigil/venv (system Python untouched).
#   3. Installs the pinned PyPI release: vigil-agent-harness==1.0.2
#   4. Symlinks the launcher to ~/.local/bin/vigil (add to PATH if missing).
#   5. Pre-installs the tirith security scanner (~/.vigil/bin/tirith, from GitHub
#      releases, SHA-256 verified) so the FIRST scanned command doesn't hit a
#      slow download. If this step fails (e.g. GitHub unreachable from your
#      network), Vigil still works — commands run with a scan-missing warning,
#      and the download retries automatically later. Re-run this script after
#      your network is fixed: it is idempotent.
#
# Never run as root. Nothing outside ~/.local, ~/.vigil is written.

set -euo pipefail

VIGIL_VERSION="1.0.2"
PYTHON_MIN="3.11"
VENV_DIR="${VIGIL_VENV_DIR:-$HOME/.local/share/vigil/venv}"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/vigil"
REPO_URL="https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/v1.0"

say()  { printf '\033[1;34m[vigil]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[vigil]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[vigil]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
if [ "$(id -u)" -eq 0 ]; then
  die "Do not run this as root. Vigil installs into your home directory only."
fi

# --- Python resolution ------------------------------------------------------
# Vigil needs Python >= 3.10. If the system python is missing or too old, we
# bootstrap a private one with uv (https://astral.sh/uv) — no root, no system
# packages, works on any distro. This is the "environment has nothing" path
# the one-liner exists for.
PYTHON_BIN="$(command -v python3 || true)"
if [ -n "$PYTHON_BIN" ]; then
  PY_MAJOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
  PY_MINOR="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"
  if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    warn "system python3 is $PY_MAJOR.$PY_MINOR — Vigil needs $PYTHON_MIN+. Bootstrapping a private Python with uv..."
    PYTHON_BIN=""
  fi
else
  warn "python3 not found — bootstrapping a private Python with uv..."
fi

if [ -z "$PYTHON_BIN" ]; then
  UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
  if [ ! -x "$UV_BIN" ] && ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (Python installer) to $HOME/.local/bin..."
    mkdir -p "$HOME/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh >/dev/null 2>&1 \
      || die "uv bootstrap failed — install Python $PYTHON_MIN+ yourself (https://www.python.org/downloads/) and re-run."
  fi
  if [ -x "$UV_BIN" ]; then UV="$UV_BIN"; else UV="$(command -v uv)"; fi
  say "Installing Python $PYTHON_MIN+ via uv (private to $HOME, system untouched)..."
  "$UV" python install "$PYTHON_MIN" >/dev/null 2>&1 \
    || die "uv python install failed — install Python $PYTHON_MIN+ yourself and re-run."
  PYTHON_BIN="$("$UV" python find "$PYTHON_MIN")"
  say "Using $( "$PYTHON_BIN" --version 2>&1 ) at $PYTHON_BIN"
fi

say "Installing Vigil $VIGIL_VERSION (venv: $VENV_DIR)"

# Existing installation guard — no silent clobber (multi-version PATH confusion is real).
if [ -x "$LAUNCHER" ]; then
  EXISTING="$("$LAUNCHER" --version 2>/dev/null | head -1 || echo unknown)"
  warn "vigil already exists at $LAUNCHER ($EXISTING)."
  warn "This script will overwrite it with $VIGIL_VERSION. Ctrl-C now to abort;"
  warn "re-run to continue in 5s..."
  sleep 5
fi

# ---------------------------------------------------------------- venv + pkg
mkdir -p "$VENV_DIR" "$BIN_DIR"
# Idempotency with a version guard: a venv left behind by an older run (or an
# older script) may carry a too-old Python — rebuild it rather than reusing it
# and letting pip fail confusingly on Requires-Python.
if [ -x "$VENV_DIR/bin/python" ]; then
  if ! "$VENV_DIR/bin/python" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
    warn "existing venv uses an old Python — rebuilding $VENV_DIR..."
    rm -rf "$VENV_DIR"
  fi
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "Creating virtualenv..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

say "Installing vigil-agent-harness==$VIGIL_VERSION (PyPI)..."
"$VENV_DIR/bin/pip" install --disable-pip-version-check "vigil-agent-harness==$VIGIL_VERSION"

# Launcher symlink (venv console script; keep it a symlink so future `pip install -U`
# inside the venv updates what PATH sees).
ln -sf "$VENV_DIR/bin/vigil" "$LAUNCHER"

say "Verifying install..."
"$LAUNCHER" --version

# ---------------------------------------------------------------- tirith preinstall
say "Pre-installing tirith security scanner (~/.vigil/bin/tirith, SHA-256 verified)..."
# ensure_installed() fires a background thread and returns immediately — the
# installer needs the SYNCHRONOUS core (_install_tirith) so success/failure is
# known before we print the summary. ~30s wall-clock budget inside; fails fast.
if timeout 180 "$VENV_DIR/bin/python" -c "from tools.tirith_security import _install_tirith; path, _reason = _install_tirith(log_failures=True); raise SystemExit(0 if path else 1)" >/dev/null 2>&1; then
  say "tirith ready."
else
  warn "tirith download failed (GitHub releases may be unreachable from your network)."
  warn "Vigil still works; the first scanned command will warn and retry. Fix your"
  warn "network (VPN / HTTPS_PROXY) and re-run this script — it is idempotent."
fi

# ---------------------------------------------------------------- done
cat <<EOF

$(printf '\033[1;32m')Vigil $VIGIL_VERSION installed.$(printf '\033[0m')

Next steps:
  1. Ensure $BIN_DIR is on your PATH:
       export PATH="\$HOME/.local/bin:\$PATH"
  2. Try it:
       vigil --help
  3. First-run orientation (sample topology + runbooks, read-only):
       vigil ops-init
     Full docs: $REPO_URL/README.md

Note: tirith may finish downloading in the background on first command run if the
pre-install above was skipped — that is normal and non-blocking.
EOF
