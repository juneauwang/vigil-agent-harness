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
PYTHON_MIN="3.10"
VENV_DIR="${VIGIL_VENV_DIR:-$HOME/.local/share/vigil/venv}"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/vigil"
REPO_URL="https://raw.githubusercontent.com/juneauwang/vigil-agent-harness/master"

say()  { printf '\033[1;34m[vigil]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[vigil]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[vigil]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
if [ "$(id -u)" -eq 0 ]; then
  die "Do not run this as root. Vigil installs into your home directory only."
fi

command -v python3 >/dev/null 2>&1 || die "python3 not found — install Python $PYTHON_MIN+ first (https://www.python.org/downloads/)."

PY_MAJOR="$(python3 -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$(python3 -c 'import sys; print(sys.version_info.minor)')"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  die "python3 is $PY_MAJOR.$PY_MINOR — Vigil needs $PYTHON_MIN+."
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
if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "Creating virtualenv..."
  python3 -m venv "$VENV_DIR"
fi

say "Installing vigil-agent-harness==$VIGIL_VERSION (PyPI)..."
"$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check "vigil-agent-harness==$VIGIL_VERSION"

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
