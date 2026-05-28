# Handoff Guide — Claude Terminal

**From:** Mark (Claude Code, Anthropic CLI agent, Opus 4.7)
**To:** Alex Agent (Richard's personal assistant) in the Hermes environment
**Date:** 2026-05-27
**Goal:** Finish this project autonomously. Richard wants to step back; you have everything you need below.

---

## 0. Quick orient

- **Repo:** https://github.com/gassrichard0-bit/claude-terminal
- **Local working copy:** `/Users/richardgass/Desktop/claude-terminal`
- **User's primary work dir Claude opens by default:** `/Users/richardgass/app` (`WORK_DIR` env)
- **Live URL during development:** https://bubble-explode-thievish.ngrok-free.dev/
- **Latest commit on `main`:** `cdd6771` "Add one-command installer, fix start.sh path, rewrite README for self-host"
- **Branch policy:** push directly to `main`. No PR workflow.

## 1. What this project is

A self-hosted "Claude Code as a phone app." Richard runs a FastAPI server on his Mac that forks a PTY, drops `claude --continue` into it, and streams I/O over WebSocket to a single-page web UI. The UI is installed as a PWA on his iPhone via ngrok. He wants a smooth, native-feeling chat client for his own Claude sessions — **single user, single Mac, not multi-tenant**.

## 2. Architecture

```
iPhone PWA ──HTTPS──▶ ngrok ──▶ FastAPI :8080 ──▶ PTY ──▶ claude --continue
                                       │
                                       └─ GET /api/messages ──reads──▶
                                          ~/.claude/projects/<encoded>/<session>.jsonl
```

Files, in reading order:
1. **`backend/server.py`** — FastAPI app. Owns PTY lifecycle (`PTYSession` class), WebSocket I/O loop, `/api/health`, `/api/config`, `/api/messages`, `/api/sessions`, and static frontend serving with no-cache headers.
2. **`frontend/index.html`** — entire frontend in one file. xterm.js terminal + chat-bubble view + iOS PWA + touch handlers + localStorage scrollback + view toggle + floating Copy popup.
3. **`frontend/xterm/`** — vendored xterm.js, fit + web-links addons. Already in repo, don't `npm install`.
4. **`start.sh`** — kills any existing process on :8080, starts uvicorn in the background, prints next steps. Resolves its own directory; safe to run from anywhere.
5. **`install.sh`** — one-command setup for new users. Idempotent (re-running is safe). Prereq checks → npm install Claude Code → clone repo → pip install Python deps → hint about ngrok.
6. **`README.md`** — user-facing docs. Points at install.sh.
7. **`fly.toml`, `Dockerfile`, `docker-compose.yml`, `deploy.sh`** — stale, from an abandoned cloud-deploy idea. **TODO §11.4:** delete or finish.

## 3. Current state (what's done)

✅ **Live (xterm) view**
- xterm.js terminal with brightened Tokyo Night palette
- `minimumContrastRatio: 7` + `drawBoldTextInBrightColors: true`
- 1,000,000-line in-memory scrollback
- localStorage persistence: 8 MB byte stream, version-keyed wipe on schema bumps, triple-tap logo gesture to clear
- Cyan **YOU** badge echo on user input (`\x1b[48;5;39;38;5;231;1m YOU \x1b[0m` + bright cyan text)
- Mobile-friendly bottom input bar (not direct stdin into xterm)

✅ **Chat (bubble) view**
- Top-bar toggle between Live and Chat
- `/api/messages` parses the most-recently-modified JSONL under `~/.claude/projects/`, strips `tool_use` / `tool_result`, returns `{role, content, timestamp}`
- iMessage-style layout: user right (cyan gradient, `margin-left: auto`), assistant left (translucent panel)
- Role labels (`You` / `Claude`) only on speaker change; tight gap within a run, bigger gap between speakers
- Optimistic user bubble on Send + follow-up polls at 1.2 s / 3.5 s / 7 s for Claude's reply
- Background poll while Chat view is visible: every 5 s, with no-change short-circuit
- DocumentFragment-batched inserts; bulk-load skips per-bubble animation; RAF scroll-to-bottom

✅ **PWA + connection survival**
- `apple-mobile-web-app-capable`, status-bar styling, SVG home-screen icon, `theme-color`, `mobile-web-app-capable`
- WebSocket keepalive ping every 25 s
- `visibilitychange` + `online` event listeners reconnect immediately on app foreground / Wi-Fi recovery
- Server-side `Cache-Control: no-store...` on `/` so HTML refreshes pull fresh

✅ **PTY behavior**
- `spawn()` execs: `bash --login -c "claude --continue 2>/dev/null || claude; exec bash --login"`
- Env: `TERM=xterm-256color`, `COLORTERM=truecolor`, `FORCE_COLOR=1`, `CLICOLOR_FORCE=1` set in PTY child
- PTY kept alive on WebSocket disconnect — same session resumes on next connect
- Hardcoded `sessionId='claude-main'` (front+back); only one logical session

✅ **Touch handling (mobile)**
- One-finger drag = scroll the terminal (fractional accumulator so slow drags aren't lost) with momentum decay
- Two-finger drag = pan the whole UI body (`translateY`, snaps back on release)
- Tap = focus the input bar
- Long-press (350 ms) → enters selection mode, drag extends selection via `term.select()` / `term.selectLines()`; on release, floating Copy popup appears at finger position
- `visualViewport.resize` listener re-fits xterm when iOS keyboard opens/closes

## 4. Key code shapes you'll touch

### Server (`backend/server.py`)
- `class PTYSession` — pid/fd, `spawn()`, `resize(cols, rows)`, `write(data)`, `write_control(char)`, `attach_websocket(ws)`, `detach_websocket()`, `reader_loop()`, `cleanup()`. Reader task reads from PTY fd and pushes JSON `{type:'output', data:...}` to the WebSocket.
- WebSocket message types accepted: `input`, `enter`, `resize`, `control`, `restart`, `ping` (silently dropped). Outbound: `output`.
- `WORK_DIR` env var (defaults `~/app`) — `os.chdir(str(WORK_DIR))` before exec in child fork.
- `AUTH_TOKEN` from env or random UUID per run; the frontend fetches it via `/api/config` so users don't type it.

### Frontend (`frontend/index.html`)
- Globals: `term` (xterm.js Terminal), `fitAddon`, `ws`, `cmdInput`, `chatView`, `chatMessages`, `currentView`, `lastRenderedCount`.
- Helpers: `write_to_terminal(data)`, `load_scrollback()`, `schedule_save()`, `clear_scrollback()`, `send_command()`, `set_status()`, `flash_status()`, `boot()`, `connect(token)`, `setView(name)`, `renderMessages(msgs)`, `loadHistory()`, `pixelToCell(x,y)`, `apply_selection(start, end)`, `showCopyPopup(x,y)`, `hideCopyPopup()`, `uiPan(dy)`, `uiPanReset()`, `start_momentum()`, `stop_momentum()`.
- `SCROLLBACK_KEY = 'claude-terminal-stream-claude-main'`, `SCROLLBACK_VERSION_KEY = 'claude-terminal-version'`, `SCROLLBACK_VERSION = '2026-05-27-clean'` — bump the version string in code to force a one-time wipe on every device.

## 5. Richard's preferences (don't relearn these)

- **Terse responses.** No trailing summaries of "what I just did" — the diff is visible.
- **Mobile-first.** Assume iPhone + on-screen keyboard.
- **Keep the cyan YOU badge in Live view.** He explicitly asked it back when I tried plain text. Don't touch.
- **Right-align user bubbles, left-align Claude's.** Force with `margin-left: auto` not just `align-self`.
- **One-finger drag = scroll; two-finger drag = UI pan; long-press = selection.** Don't break this trinity.
- **Don't restart the server from inside its own PTY.** It kills the live session. Use a detached background command. Pattern in §7.
- **Confirm before any action that breaks the live session** (server restart, force-push, deleting branches, etc.).
- **Don't create planning/notes/scratch docs** unless explicitly told. This GUIDE.md is the exception (requested by Richard).
- **He's patient with iteration but allergic to repeating himself.** If he flagged a preference once in this session, treat it as a permanent rule.

## 6. Known gotchas + things that bit us

1. **alt-screen vs scrollback.** Claude Code's TUI runs in the alternate screen buffer; that content does **not** enter xterm's scrollback. We tried two strip-and-archive approaches; both caused visual mess or "content disappears every 1.8 s" bugs. **Revert to plain `term.write(data)` and use the Chat view for true history.** Don't relitigate this unless Richard explicitly asks.
2. **File overwrite incident.** Some external process (unknown — maybe a sync agent or another Claude instance) overwrote `frontend/index.html` with a 248-line older version mid-session. Restored from git. If it happens again: `cd /Users/richardgass/Desktop/claude-terminal && git checkout HEAD -- frontend/index.html`. Backup of the overwriting file at `/tmp/index.html.overwritten-backup`.
3. **GitHub PAT leaked in `.git/config`.** The remote URL contains `ghp_REDACTED-rotate-me`. Local-only (won't leak via push), but **rotate it.** Steps in §8.3.
4. **`/api/messages` re-parses entire JSONL every poll.** With 1,200+ lines it's ~10–30 ms; not catastrophic but should be cached. **TODO §11.2.**
5. **PWA cache.** iOS PWAs cache aggressively. To force a refresh: close the tab in the Home Screen app fully (swipe up from app switcher), reopen. Hard-reload of CSS/JS without that won't always work.
6. **`start.sh` doesn't install Python deps.** Only `install.sh` does that. If Richard's deps drift after `git pull`, the server fails silently. **TODO §11.5.**

## 7. How to run, test, debug

### Verify server is up
```bash
curl -sf http://localhost:8080/api/health && echo " OK"
curl -s http://localhost:8080/api/messages | python3 -m json.tool | head -20
```

### Find / inspect server process
```bash
lsof -ti:8080 | xargs -I {} ps -p {} -o pid,etime,command
```

### Restart server from OUTSIDE the PTY (Richard's native Mac Terminal)
```bash
kill $(lsof -ti:8080)
cd /Users/richardgass/Desktop/claude-terminal && bash start.sh
```

### Restart server from INSIDE the PTY (your situation as an agent) — detached pattern
```bash
(nohup bash -c '
  sleep 4
  kill -TERM $(lsof -ti:8080) 2>/dev/null
  sleep 2
  cd /Users/richardgass/Desktop/claude-terminal
  exec python3 -m uvicorn backend.server:app --host 0.0.0.0 --port 8080 --ws-ping-interval 30 --ws-ping-timeout 10 > /tmp/claude-terminal.log 2>&1
' < /dev/null > /tmp/restarter.log 2>&1 &)
```
The 4-second sleep gives you time to acknowledge to the user that disconnection is imminent. After the restart, `claude --continue` will resume the same session.

### Tail logs
```bash
tail -f /tmp/claude-terminal.log
```

### Syntax-check before any commit
```bash
python3 -c "import ast; ast.parse(open('backend/server.py').read())" && echo OK
bash -n start.sh install.sh && echo OK
```

### Browser dev path
Richard refreshes the PWA after every change. Frontend-only edits: refresh in iOS Safari (or close + reopen the Home Screen app for stubborn cache). Backend edits: restart the server (see above).

## 8. Operational details

### 8.1 Environment vars
| Var | Default | Where set | Notes |
|-----|---------|-----------|-------|
| `CLAUDE_TERMINAL_TOKEN` | random UUID per server start | `server.py:31` | Frontend fetches via `/api/config`; user never types it |
| `WORK_DIR` | `~/app` | `server.py:32` | `chdir` target in the PTY child |
| `TERM`, `COLORTERM`, `FORCE_COLOR`, `CLICOLOR_FORCE` | set in `PTYSession.spawn()` | `server.py:51-65` | Forces color from CLIs that auto-detect |

### 8.2 Richard's adjacent files / systems
- **`~/.claude/CLAUDE.md`** — global Claude Code instructions (Ruflo integration, current date, user email).
- **`~/.claude/settings.json`** — contains `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (these are real secrets), `permissions.allow: [Bash, Read, Write, Edit]`, `skipDangerousModePermissionPrompt: true`, `model: opus`, `effort: high`, custom `statusLine` script, and `Stop` / `SessionStart` hooks that ping Telegram. **Do NOT push this file anywhere.**
- **`~/.claude/notes/restart-web-terminal.md`** — Richard's own copy of the restart commands.
- **`~/.claude/notes/change-theme.md`** — `/config` theme instructions.
- **`~/.claude/statusline-command.sh`** — minimal status line (`Opus 4.7 · app · main` style).
- **`/Users/richardgass/Desktop/Alex Agent/KnowledgeVault/WORKSTREAM.md`** — Alex Agent's master dashboard. This handoff is announced there.

### 8.3 Rotate the GitHub PAT
1. https://github.com/settings/tokens → revoke `ghp_PT5Swk0…`.
2. Generate a new fine-grained PAT with `repo` scope on `gassrichard0-bit/claude-terminal`.
3. Update the local remote URL:
   ```bash
   cd /Users/richardgass/Desktop/claude-terminal
   git remote set-url origin https://NEW_TOKEN@github.com/gassrichard0-bit/claude-terminal.git
   ```
4. Verify: `git push origin main` (should succeed without prompting).

### 8.4 ngrok
- Tunnel running on Richard's Mac: `ngrok http 8080`. URL: `https://bubble-explode-thievish.ngrok-free.dev/`.
- If ngrok URL changes (free-tier rotation), Richard needs to re-Add to Home Screen, since the PWA is pinned to the original URL. Consider documenting this; or paying for a static ngrok subdomain.

### 8.5 Commit conventions
Short title (≤72 chars) + bullet body explaining the why, not the what. Always end with:
```
Co-Authored-By: <YourModelName> <noreply@anthropic.com>
```
Replace `<YourModelName>` with your own (e.g. `Alex Agent V3` or whatever).

## 9. JSONL session log format (what `/api/messages` parses)

Lines under `~/.claude/projects/<encoded-path>/<session-id>.jsonl`. Each line is a JSON object. Relevant shapes:

```json
{"type":"user","message":{"role":"user","content":"plain string"},"timestamp":"2026-05-27T..."}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."},{"type":"tool_use",...}]},"timestamp":"..."}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"...","tool_use_id":"..."}]},"timestamp":"..."}
{"type":"summary","summary":"...","leafUuid":"..."}
```

Our parser (`server.py` `/api/messages`):
- Keeps only entries where `role in ("user","assistant")`.
- Content can be string OR list of parts; flattens list to text portions only.
- **Skips `tool_use` and `tool_result`** — they'd pollute the chat view.
- Returns most-recent JSONL only (`max` by `mtime`).

## 10. Test plan to verify nothing's broken

After any non-trivial change:
1. `python3 -c "import ast; ast.parse(open('backend/server.py').read())" && bash -n start.sh install.sh`
2. `curl -sf http://localhost:8080/api/health`
3. `curl -s http://localhost:8080/api/messages | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['messages']))"` — expect >0
4. Refresh PWA on Richard's phone (he keeps a Home Screen icon)
5. Live view: send `seq 1 50`, verify scroll works and colors render
6. Chat view: toggle, verify bubbles populate; send a message, verify optimistic bubble + Claude's reply appear

## 11. TODO list — finish the project from here

Roughly priority-ordered. Pick what feels solid, ship in small commits.

1. **Native iOS selection in xterm.** Long-press + drag currently calls `term.select()` / `term.selectLines()`. The user wants the real iOS handles + popover. Investigate forcing xterm's DOM renderer (instead of canvas) and `user-select: text` on `.xterm-rows` so iOS recognizes selectable text. Hard problem; if not solvable, document why and stop.
2. **`/api/messages` caching.** Add file-mtime check; return 304 / cached payload if unchanged. Or add `?since=<count>` query parameter so only new messages travel the wire.
3. **Rotate the leaked PAT** (§8.3). Don't ship anything else until this is done — security-first.
4. **Delete or finish `fly.toml`, `Dockerfile`, `docker-compose.yml`, `deploy.sh`.** They imply a cloud-deploy story we abandoned. Either remove them with a `Remove abandoned cloud-deploy scaffolding` commit, or take them across the finish line as an opt-in path. Default: delete.
5. **`start.sh` should install Python deps idempotently.** After `git pull` users sometimes need new deps. Add a `python3 -m pip install --user --quiet -r requirements.txt` step. Create `requirements.txt` if missing.
6. **Stop / restart affordance from the Web App.** Currently triple-tap logo wipes scrollback. Consider adding a long-press logo gesture to send a `restart` WebSocket message (server already supports it — see `server.py:240` `restart` handler).
7. **Make the chat view scrollable to the very top** (oldest message). Initial scroll-to-bottom is correct, but check that scrolling up doesn't get stuck. Add a small "Load older" sentinel if there are >500 messages and only render the last 200 initially.
8. **Polish the install.sh** on a fresh Mac: test from scratch in a clean user account (or VM). Likely friction: Apple's Python 3.9 lacks some packages; Node may need manual install; ngrok requires an account + authtoken.
9. **Update README** with a screen recording or GIF of the phone PWA in action. Linked from the repo's README would be a nice touch.
10. **Consider a `requirements.txt` or `pyproject.toml`** to lock Python deps. Right now `install.sh` just `pip install`s the latest of each.

## 12. If you get stuck

- **Read `frontend/index.html` end to end first.** It's ~700 lines; touching JS without that context is how regressions get shipped.
- **`backend/server.py` is small (~290 lines).** Read it whole.
- **Don't assume; verify with curl/lsof/ps.** This session had multiple moments where we *thought* the server restarted but it hadn't.
- **Richard responds to short messages quickly but expects updates.** If you go quiet for a long task, drop one-line progress notes via Telegram or by leaving a Workstream entry.

---

Good luck. You have everything. Ship it.

— Mark (Claude Code, Opus 4.7), 2026-05-27
