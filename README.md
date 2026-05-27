# Claude Terminal

A self-hosted Claude Code terminal you open in your browser.
Works on phone, tablet, laptop — anything with a browser.

## Architecture

```
Browser (xterm.js) → WebSocket → FastAPI Server → PTY → Claude Code CLI
```

- **Frontend:** Single HTML page with xterm.js — mobile keyboard bar included
- **Backend:** FastAPI + Python PTY integration — forks a real pseudo-terminal
- **Claude Code:** Runs in the PTY as if you'd opened a terminal

## Quick Start

### Local development

```bash
# Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# Start
docker compose up --build
# Open http://localhost:8080
# Token: dev-token-change-me
```

### Deploy to Fly.io (free tier)

```bash
# Set your API key and deploy
ANTHROPIC_API_KEY=sk-ant-... ./deploy.sh

# Open the URL it prints, paste your token, you're in.
```

### Or manually

```bash
flyctl launch --region iad --no-deploy
flyctl secrets set CLAUDE_TERMINAL_TOKEN=$(uuidgen) ANTHROPIC_API_KEY=sk-ant-...
flyctl deploy --remote-only
```

## Configuration

| Env Var | Required | Description |
|---------|----------|-------------|
| `CLAUDE_TERMINAL_TOKEN` | ✅ | Auth token shared between server and browser |
| `ANTHROPIC_API_KEY` | ✅ | Your Anthropic API key for Claude Code |
| `WORK_DIR` | ❌ | Working directory for claude (default: /home/app) |

## Cost

- **Fly.io free tier:** $0/mo — stays up 24/7, 256MB RAM, 3GB storage
- **Anthropic API:** You pay per token — ~$3-5/hr for heavy Claude Code use
- **Your own server:** $0/mo if you run it on existing hardware

## Features

- Persistent sessions — close browser, reopen, pick up where you left off
- Mobile keyboard bar — auto-shows on phones
- Dark terminal theme (Claude terminal colors)
- WebSocket reconnect — survives network blips
- Restart button — kill and restart Claude Code in-session
