# Claude Terminal — Friend Setup Guide

> **For the assistant reading this:** You are helping your user (the "friend") set up Claude Terminal on **their own Mac**. Everything in this guide happens on the friend's machine. Do not touch any other machine, do not push to the upstream repo, do not use anyone else's credentials.

## What this is

Claude Terminal is a self-hosted phone app for Claude Code. The friend installs a small FastAPI server on their Mac that forks a `claude --continue` PTY and streams I/O over WebSocket to a single-page web UI. They install the page as a PWA on their phone over an ngrok tunnel.

The upstream repo is `gassrichard0-bit/claude-terminal`. The friend will clone it, run it locally, and expose it via their own ngrok account. **Their Mac, their files, their Claude session, their ngrok tunnel.** Nothing they do here touches the upstream maintainer's setup.

## What the friend needs ahead of time

- macOS (or Linux) with admin rights
- Node 18+ and Python 3.9+ installed
- An Anthropic API key OR a Claude Code OAuth login already set up on this Mac
- An ngrok account (free tier is fine) and the `ngrok` CLI installed
- An iPhone or Android phone on the same Apple ID / Google account they'll install the PWA on

If any of these are missing, install them first:

```bash
# macOS
brew install python node ngrok
```

For ngrok, sign up at https://ngrok.com, copy the auth token, and run:

```bash
ngrok config add-authtoken <THEIR_TOKEN>
```

## Step 1 — Run the installer

```bash
curl -fsSL https://raw.githubusercontent.com/gassrichard0-bit/claude-terminal/main/install.sh | bash
```

This clones into `~/claude-terminal`, installs `@anthropic-ai/claude-code` globally if missing, and installs the Python deps (fastapi, uvicorn, websockets, pydantic). It does **not** start the server — just sets up files.

If Claude Code isn't already logged in on this Mac, log in once interactively:

```bash
claude
# follow the OAuth prompt, then exit
```

## Step 2 — Start the backend

```bash
cd ~/claude-terminal
bash start.sh
```

You should see `✅ Claude Terminal running on http://localhost:8080`. Verify with:

```bash
curl -s http://localhost:8080/api/health
# expect: {"status":"ok","sessions":[]}
```

If the health check fails, tail the log:

```bash
tail -50 /tmp/claude-terminal.log
```

## Step 3 — Expose it via ngrok

In a separate terminal:

```bash
ngrok http 8080
```

Copy the printed `https://<random>.ngrok-free.dev` URL. This is the friend's **personal** tunnel — only their Mac is on the other end of it.

## Step 4 — Install the PWA on the friend's phone

1. Open the ngrok URL in Safari (iOS) or Chrome (Android) on the friend's phone.
2. The setup screen asks for a "Backend URL" — paste the same ngrok URL there.
3. Tap **Connect**. The terminal should load Claude's TUI.
4. iOS: Share → **Add to Home Screen**. Android: ⋮ → Install app.
5. From now on the icon opens fullscreen with no browser chrome.

## Step 5 — Keep it running

- The Python server keeps running until `start.sh` is killed or the Mac sleeps. Re-run `bash start.sh` to restart (it's idempotent — it kills any old process on 8080 first).
- ngrok free tunnels rotate URLs on every restart. The friend needs to either keep ngrok running, pay for a static domain, or update the Backend URL in Settings ⚙ each time.
- Don't run `claude` in another terminal at the same time on the same project, or both sessions will fight over the same JSONL.

## Stopping it

```bash
kill $(lsof -ti:8080)        # stop the backend
# Ctrl-C in the ngrok terminal stops the tunnel
```

## Constraints — what the assistant MUST NOT do

- Don't push to `gassrichard0-bit/claude-terminal`. If the friend wants to modify the code, fork it under their own GitHub account first.
- Don't read or modify `~/.claude/settings.json` beyond what install.sh requires — the friend may have their own hooks, theme, or API key in there.
- Don't touch any SSH config, GitHub credentials, or ngrok tokens belonging to anyone other than the friend.
- Don't background-restart the server from inside a session that's running through it — kills the live PTY. If a restart is needed, use a detached script: `(nohup bash -c '...' < /dev/null > /tmp/r.log 2>&1 &)`.

## Reference

- Upstream repo: https://github.com/gassrichard0-bit/claude-terminal
- Health endpoint: `GET /api/health`
- Conversation history endpoint: `GET /api/messages?last=200` (reads `~/.claude/projects/*/*.jsonl`)
- WebSocket: `WS /ws/claude-main?token=<auth-token>` — token is auto-generated and exposed via `/api/config`

If anything in this guide is ambiguous, ask the friend before guessing.
