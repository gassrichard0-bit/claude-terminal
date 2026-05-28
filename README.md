# Claude Terminal

A self-hosted, phone-friendly Claude Code terminal you open in your browser. Runs entirely on your own Mac (or Linux box) — your files, your shell, your Claude session, never leaves your machine. Use it from your phone over ngrok like a remote, polished Claude app.

**Share the UI, keep the backend personal.** Anyone can open the same frontend URL on their phone — when they first launch it, they enter **their own** Mac's ngrok URL. The app then talks to *their* Mac, not the one hosting the page. Every user runs their own `claude` on their own machine; the shared web app is just the polished communication layer.

![architecture](https://img.shields.io/badge/runs%20on-your%20Mac-blueviolet) ![python](https://img.shields.io/badge/python-3.9%2B-3776ab) ![status](https://img.shields.io/badge/status-personal%20tool-success)

## What you get

- **xterm.js terminal** in the browser — full ANSI colors, Tokyo Night theme, animated spinner, all of Claude Code's UI as-is
- **Chat-bubble view** — toggle to a clean iMessage-style view of your conversation, reading directly from `~/.claude/projects/`
- **PWA**: Add to Home Screen on iOS / Android and it opens like a native app — no Safari bars
- **Touch-friendly**: one-finger scroll, long-press to select, floating Copy popup, mobile keyboard bar
- **Auto-resume**: every connection runs `claude --continue` so your session picks up exactly where you left off
- **Persistent scrollback** in localStorage so closing the app doesn't wipe history

## One-command install (macOS / Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/gassrichard0-bit/claude-terminal/main/install.sh | bash
```

This script:
1. Checks Python 3.9+ and Node 18+ are installed
2. Installs Claude Code CLI globally (`npm i -g @anthropic-ai/claude-code`)
3. Clones the repo into `~/claude-terminal`
4. Installs the Python deps (fastapi, uvicorn, websockets)
5. Reminds you to install ngrok if you don't have it

## Manual install

```bash
# 1. Prereqs (macOS)
brew install python node ngrok
npm install -g @anthropic-ai/claude-code

# 2. Clone
git clone https://github.com/gassrichard0-bit/claude-terminal.git ~/claude-terminal
cd ~/claude-terminal

# 3. Python deps
python3 -m pip install --user fastapi uvicorn 'uvicorn[standard]' websockets pydantic
```

## Running it

```bash
cd ~/claude-terminal
bash start.sh                  # starts server in background on :8080
```

In a second terminal:
```bash
ngrok http 8080
```

Copy the printed `https://*.ngrok-free.dev` URL onto your phone:
1. Open in Safari (iOS) or Chrome (Android)
2. Share → **Add to Home Screen**
3. Tap the new icon — it opens fullscreen, no browser chrome

## Architecture

```
Phone browser (PWA) ── HTTPS ──▶ ngrok ──▶ FastAPI :8080 ──▶ PTY ──▶ Claude Code
                                                                     │
                                                                     ▼
                                                          ~/.claude/projects/*.jsonl
                                                          (session log, served as
                                                           chat bubbles via /api/messages)
```

- **Frontend** (`frontend/index.html`): xterm.js + a hand-rolled chat-bubble view, plus iOS PWA meta tags. All in one HTML file.
- **Backend** (`backend/server.py`): FastAPI, forks a real PTY, streams I/O over WebSocket, exposes `/api/messages` to read the conversation log.

## Configuration

| Env var                | Default                       | What it does                                       |
| ---------------------- | ----------------------------- | -------------------------------------------------- |
| `CLAUDE_TERMINAL_TOKEN`| random UUID per run           | Token in the WS URL; the web app auto-discovers it |
| `WORK_DIR`             | `~/app`                       | Directory Claude opens by default                  |

## Notes & gotchas

- **Single user, single machine.** The PTY is shared by every connection to your server. Don't share your ngrok URL with anyone you wouldn't give shell access to.
- The chat view reads the **most recently modified** JSONL under `~/.claude/projects/`, which means it follows whichever Claude session is freshest.
- Triple-tap the `⚡ MAC` logo to wipe localStorage scrollback if it ever gets messy.
- The Python server keeps the PTY alive across WebSocket disconnects — close the browser and reconnect later, same Claude session.

## Updating

```bash
cd ~/claude-terminal && git pull && bash start.sh
```

## Stopping

```bash
kill $(lsof -ti:8080)
```

## License

MIT.
