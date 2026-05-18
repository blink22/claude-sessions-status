#!/usr/bin/env bash
# claude-sessions-status — one-shot installer for users without Homebrew.
#
# Clones the repo to ~/.claude-sessions-status/, symlinks the entry points
# into ~/.local/bin/, then runs the interactive setup.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Blink22/claude-sessions-status/main/install.sh | bash
#
# Or, from a local checkout:
#   ./install.sh
#
# Environment overrides:
#   CSS_REPO   - git URL to clone (default: GitHub https URL)
#   CSS_REF    - branch/tag/commit to check out (default: main)
#   CSS_DIR    - install root (default: ~/.claude-sessions-status)
#   CSS_BIN    - directory for entry-point symlinks (default: ~/.local/bin)

set -euo pipefail

REPO="${CSS_REPO:-https://github.com/Blink22/claude-sessions-status.git}"
REF="${CSS_REF:-main}"
INSTALL_DIR="${CSS_DIR:-$HOME/.claude-sessions-status}"
BIN_DIR="${CSS_BIN:-$HOME/.local/bin}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# -------- Pre-flight --------
if [[ "$(uname -s)" != "Darwin" ]]; then
  red "claude-sessions-status only runs on macOS. Detected: $(uname -s)"
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  red "git is required but not installed. Install Xcode Command Line Tools:"
  red "  xcode-select --install"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  red "python3 is required but not installed."
  exit 1
fi

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
if [[ "$PY_OK" != "1" ]]; then
  red "Python 3.9+ required (have $(python3 --version))."
  exit 1
fi

# -------- Clone / update --------
bold "→ Installing claude-sessions-status to $INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "  Existing checkout found — updating…"
  git -C "$INSTALL_DIR" fetch --quiet origin
  git -C "$INSTALL_DIR" checkout --quiet "$REF"
  git -C "$INSTALL_DIR" pull --quiet --ff-only origin "$REF" 2>/dev/null || true
else
  git clone --quiet --depth=1 --branch "$REF" "$REPO" "$INSTALL_DIR"
fi
green "  ✓ Source at $INSTALL_DIR"

# -------- Symlink entry points to ~/.local/bin --------
mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/scripts/install.py"    "$BIN_DIR/claude-sessions-status"
ln -sf "$INSTALL_DIR/scripts/dashboard.py"  "$BIN_DIR/claude-sessions-status-dashboard"
chmod +x "$INSTALL_DIR/scripts/install.py" "$INSTALL_DIR/scripts/dashboard.py" \
         "$INSTALL_DIR/scripts/menubar.py" 2>/dev/null || true
green "  ✓ Symlinked entry points into $BIN_DIR"

# -------- PATH hint --------
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    bold "→ Add $BIN_DIR to your PATH so the commands are reachable."
    echo "  Add this line to your ~/.zshrc or ~/.bashrc:"
    echo "    export PATH=\"$BIN_DIR:\$PATH\""
    echo
    ;;
esac

# -------- Hand off to the Python installer --------
echo
bold "→ Launching interactive setup…"
exec "$INSTALL_DIR/scripts/install.py" install
