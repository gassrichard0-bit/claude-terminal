#!/bin/bash
# Claude Terminal — one-command installer for macOS / Linux.
#
#   Usage:
#     curl -fsSL https://raw.githubusercontent.com/gassrichard0-bit/claude-terminal/main/install.sh | bash
#
#   Or after cloning the repo:
#     ./install.sh

set -e

REPO_URL="https://github.com/gassrichard0-bit/claude-terminal.git"
INSTALL_DIR="${CLAUDE_TERMINAL_DIR:-$HOME/claude-terminal}"

# ANSI colors for nicer output
BOLD='\033[1m'; CYAN='\033[36m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; DIM='\033[2m'; RESET='\033[0m'

say()  { printf "${CYAN}%s${RESET}\n" "$*"; }
ok()   { printf "${GREEN}✓ %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}! %s${RESET}\n" "$*"; }
err()  { printf "${RED}✗ %s${RESET}\n" "$*"; }
hr()   { printf "${DIM}────────────────────────────────────────${RESET}\n"; }

hr
echo -e "${BOLD}Claude Terminal installer${RESET}"
hr

# --- 1. Prerequisites -----------------------------------------------------
say "Checking prerequisites…"

command -v git >/dev/null     || { err "git not found.  Install Xcode CLT or apt-get install git."; exit 1; }
command -v python3 >/dev/null || { err "python3 not found.  Install Python 3.9+."; exit 1; }
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "git, python3 ($PY_VER) present"

if ! command -v node >/dev/null; then
  warn "node not found.  Claude Code requires Node 18+."
  echo "    Install from https://nodejs.org or:"
  echo "      brew install node      (macOS)"
  echo "      sudo apt install nodejs npm   (Debian/Ubuntu)"
  exit 1
fi
NODE_VER=$(node --version)
ok "node $NODE_VER present"

# --- 2. Claude Code CLI ---------------------------------------------------
if ! command -v claude >/dev/null; then
  say "Installing Claude Code CLI globally…"
  npm install -g @anthropic-ai/claude-code
  ok "Claude Code installed"
else
  ok "Claude Code already installed ($(claude --version 2>/dev/null | head -1))"
fi

# --- 3. Clone or update the repo -----------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating existing checkout at $INSTALL_DIR…"
  git -C "$INSTALL_DIR" pull --ff-only
else
  say "Cloning into $INSTALL_DIR…"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
ok "Source up to date"

# --- 4. Python dependencies ----------------------------------------------
say "Installing Python dependencies (fastapi, uvicorn, websockets)…"
python3 -m pip install --user --quiet --upgrade pip
python3 -m pip install --user --quiet fastapi uvicorn 'uvicorn[standard]' websockets pydantic
ok "Python deps installed"

# --- 5. ngrok (optional but recommended for phone access) ----------------
if ! command -v ngrok >/dev/null; then
  warn "ngrok not found — needed if you want to reach the server from your phone outside your LAN."
  echo "    Install with:"
  echo "      brew install ngrok          (macOS)"
  echo "      https://ngrok.com/download  (other platforms)"
  echo "    Then sign up at https://ngrok.com and run:  ngrok config add-authtoken <your-token>"
else
  ok "ngrok present"
fi

# --- 6. Done --------------------------------------------------------------
hr
echo -e "${BOLD}${GREEN}Install complete.${RESET}"
hr
echo
echo "To start the server:"
echo "  cd \"$INSTALL_DIR\" && bash start.sh"
echo
echo "To expose it to your phone (in a second terminal):"
echo "  ngrok http 8080"
echo
echo "Then open the printed https://*.ngrok-free.dev URL on your phone and"
echo "tap Share → Add to Home Screen to install it as an app."
echo
hr
