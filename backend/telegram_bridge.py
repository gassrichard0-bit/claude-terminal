"""Telegram ↔ Claude Code bridge.

Lets you chat with `claude --continue` from Telegram by piggy-backing on
the same backend WebSocket the PWA uses. The PWA and the bridge attach
to the same PTY session, so the conversation is shared — sending from
Telegram appears in the PWA terminal and vice versa.

How it runs:
    python3 -m backend.telegram_bridge

Config (env vars or backend/server.py defaults):
    TELEGRAM_BOT_TOKEN       — your Telegram bot's HTTP API token
    CLAUDE_TERMINAL_TOKEN    — the backend AUTH_TOKEN (so the WS accepts us)
    CLAUDE_TERMINAL_HOST     — defaults to 127.0.0.1:8080
    BRIDGE_ALLOWED_CHAT_IDS  — comma-separated; defaults to admin chat + every
                               user with a telegram_chat_id from users.json

Chat UX:
    - User sends a message on Telegram → bridge writes it to the PTY as input
    - Bridge captures PTY output, strips ANSI codes, edits a single 'thinking'
      message progressively as content streams in (≈ once per 2s).
    - When output goes quiet for IDLE_SETTLE_SECONDS, that message is finalized
      and the next user input starts a fresh reply message.
    - Long replies are split across multiple Telegram messages (Telegram caps
      at 4096 chars per message).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# macOS's DNS cache on some networks only returns IPv6 for api.telegram.org,
# and stock IPv4-only Wi-Fi can't reach the v6 endpoint → urllib hangs/errors.
# Force the v4 address for known Telegram API hosts.
_TG_V4 = {
    "api.telegram.org": "149.154.167.220",
}
_orig_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host in _TG_V4:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_TG_V4[host], port))]
    return _orig_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo

# Reuse the auth module so we read the same users file the server does
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.auth import UserDB  # noqa: E402

try:
    import websockets
except ImportError:
    print("error: websockets module missing. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)


# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BACKEND_TOKEN = os.environ.get("CLAUDE_TERMINAL_TOKEN", "")
BACKEND_HOST = os.environ.get("CLAUDE_TERMINAL_HOST", "127.0.0.1:8080")
SESSION_ID = os.environ.get("BRIDGE_SESSION_ID", "claude-main")
POLL_TIMEOUT_SECONDS = 30                # long-poll Telegram getUpdates
IDLE_SETTLE_SECONDS = 2.0                # Claude output considered "done" after this much silence
TELEGRAM_MSG_MAX = 4000                  # safe under 4096
EDIT_THROTTLE_SECONDS = 1.5              # don't edit Telegram messages faster than this


# --- helpers ---
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def strip_ansi(s: str) -> str:
    """Remove ANSI/OSC escapes — Telegram can't render them."""
    return ANSI_RE.sub("", s)


def chunk(s: str, n: int) -> list[str]:
    """Split a long string into Telegram-sized chunks."""
    return [s[i:i + n] for i in range(0, len(s), n)] or [s]


def telegram(method: str, **fields):
    """One-shot blocking call to the Telegram bot API. Returns parsed JSON."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_SECONDS + 5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"telegram {method} failed: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def get_allowed_chat_ids() -> set[str]:
    explicit = os.environ.get("BRIDGE_ALLOWED_CHAT_IDS", "").strip()
    if explicit:
        return {x.strip() for x in explicit.split(",") if x.strip()}
    db = UserDB.load()
    allowed: set[str] = set()
    if db.admin.telegram_chat_id:
        allowed.add(str(db.admin.telegram_chat_id))
    for u in db.users.values():
        if u.telegram_chat_id:
            allowed.add(str(u.telegram_chat_id))
    return allowed


# --- bridge core ---

class ChatState:
    """Per-Telegram-chat state: the in-progress reply message and last edit time."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.buffer = ""                  # accumulated reply text since last user msg
        self.message_id: Optional[int] = None  # Telegram message we're editing
        self.last_edit = 0.0
        self.idle_task: Optional[asyncio.Task] = None
        self.last_chunk_at = 0.0
        self.chunk_idx = 0                 # which 4000-char chunk are we filling

    def reset_for_new_turn(self):
        self.buffer = ""
        self.message_id = None
        self.last_edit = 0.0
        self.chunk_idx = 0
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None


class Bridge:
    def __init__(self):
        self.allowed = get_allowed_chat_ids()
        self.states: dict[str, ChatState] = {}
        self.ws = None
        self.last_update_id = 0

    def state(self, chat_id: str) -> ChatState:
        s = self.states.get(chat_id)
        if s is None:
            s = ChatState(chat_id)
            self.states[chat_id] = s
        return s

    async def connect_ws(self):
        url = f"ws://{BACKEND_HOST}/ws/{SESSION_ID}?token={BACKEND_TOKEN}"
        print(f"bridge: connecting to {url}", flush=True)
        # Aggressive keepalive so half-dead TCP gets detected fast.
        self.ws = await websockets.connect(
            url, ping_interval=15, ping_timeout=8, close_timeout=3,
        )
        print("bridge: ws connected", flush=True)

    async def ensure_ws(self):
        """If the WS is missing or closed, (re)connect. Caller awaits."""
        try:
            if self.ws is not None and not self.ws.closed:
                return
        except AttributeError:
            pass
        for attempt in range(1, 6):
            try:
                await self.connect_ws()
                return
            except Exception as e:
                print(f"bridge: ws reconnect attempt {attempt} failed: {e}", file=sys.stderr)
                await asyncio.sleep(min(2 ** attempt, 15))

    async def send_input(self, text: str):
        """Send a user message into the PTY as keystrokes + Enter. Auto-reconnect on failure."""
        for attempt in range(2):
            await self.ensure_ws()
            if self.ws is None:
                return
            try:
                await self.ws.send(json.dumps({"type": "input", "data": text}))
                await self.ws.send(json.dumps({"type": "enter"}))
                return
            except Exception as e:
                print(f"bridge: send_input retry ({attempt}): {e}", file=sys.stderr)
                self.ws = None
                continue

    async def ws_reader_loop(self):
        """Read PTY output and route it to active chats. Reconnects on WS death."""
        while True:
            await self.ensure_ws()
            if self.ws is None:
                await asyncio.sleep(2)
                continue
            try:
                async for raw in self.ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") != "output":
                        continue
                    data = strip_ansi(msg.get("data", ""))
                    if not data:
                        continue
                    for st in self.states.values():
                        if st.message_id is None and not st.buffer:
                            continue
                        st.buffer += data
                        await self._maybe_edit(st)
            except Exception as e:
                print(f"bridge: ws reader closed ({e}); reconnecting", file=sys.stderr)
                self.ws = None
                await asyncio.sleep(1)

    async def _maybe_edit(self, st: ChatState):
        """Throttled progressive edit of the active reply message."""
        now = time.time()
        if now - st.last_edit < EDIT_THROTTLE_SECONDS:
            # Reschedule a flush after the throttle interval
            if st.idle_task is None or st.idle_task.done():
                st.idle_task = asyncio.create_task(self._idle_flush(st))
            return
        await self._flush_edit(st)

    async def _idle_flush(self, st: ChatState):
        try:
            await asyncio.sleep(EDIT_THROTTLE_SECONDS)
            await self._flush_edit(st)
            # After the edit, wait for the longer settle window to finalize
            await asyncio.sleep(IDLE_SETTLE_SECONDS)
            # If no further bytes arrived during settle window, finalize the turn
            elapsed = time.time() - st.last_edit
            if elapsed >= IDLE_SETTLE_SECONDS:
                # Reset so the next user message starts fresh
                st.reset_for_new_turn()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"bridge: idle flush error: {e}", file=sys.stderr)

    async def _flush_edit(self, st: ChatState):
        """Send the buffer to Telegram — edit existing message, or send a new one
        if we crossed the 4000-char limit."""
        if not st.buffer.strip():
            return
        st.last_edit = time.time()

        # Split buffer into chunks. Each chunk_idx corresponds to a separate
        # Telegram message; we edit the most recent one as bytes accumulate.
        chunks = chunk(st.buffer, TELEGRAM_MSG_MAX)
        # Ensure we have a message for every chunk
        if st.message_id is None:
            r = telegram("sendMessage", chat_id=st.chat_id, text=chunks[0] or "(thinking…)")
            if r.get("ok"):
                st.message_id = r["result"]["message_id"]
            return
        # Edit the latest chunk's message; send new messages for any extra chunks
        # we don't yet have a message_id for.
        # For simplicity we just keep editing the same message_id with the FULL
        # buffer — if it exceeds 4000 chars, we send overflow as new messages.
        current = chunks[0]
        if len(chunks) == 1:
            telegram("editMessageText", chat_id=st.chat_id, message_id=st.message_id, text=current)
        else:
            # Cap the existing message at the first chunk, then post the rest
            telegram("editMessageText", chat_id=st.chat_id, message_id=st.message_id, text=current)
            for i in range(1, len(chunks)):
                # Need a new message for this overflow chunk
                if i > st.chunk_idx:
                    r = telegram("sendMessage", chat_id=st.chat_id, text=chunks[i])
                    if r.get("ok"):
                        st.message_id = r["result"]["message_id"]
                        st.chunk_idx = i

    async def handle_telegram_message(self, update: dict):
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if chat_id not in self.allowed:
            print(f"bridge: ignored message from unauthorized chat_id={chat_id}", flush=True)
            return

        # Slash commands
        if text == "/start":
            telegram("sendMessage", chat_id=chat_id, text="Chat with Claude here. Send any message and I'll forward it to your claude --continue session.")
            return
        if text == "/ping":
            telegram("sendMessage", chat_id=chat_id, text="pong")
            return

        # Start a fresh reply turn
        st = self.state(chat_id)
        st.reset_for_new_turn()
        # Send "thinking" placeholder
        r = telegram("sendMessage", chat_id=chat_id, text="💭 …")
        if r.get("ok"):
            st.message_id = r["result"]["message_id"]
            st.last_edit = time.time()

        # Forward to the PTY
        await self.send_input(text)

    async def telegram_poll_loop(self):
        while True:
            try:
                r = telegram("getUpdates", offset=self.last_update_id + 1, timeout=POLL_TIMEOUT_SECONDS)
                if not r.get("ok"):
                    await asyncio.sleep(2)
                    continue
                for update in r.get("result", []):
                    self.last_update_id = max(self.last_update_id, update.get("update_id", 0))
                    await self.handle_telegram_message(update)
            except Exception as e:
                print(f"bridge: poll error: {e}", file=sys.stderr)
                await asyncio.sleep(2)

    async def run(self):
        if not TELEGRAM_BOT_TOKEN:
            print("error: TELEGRAM_BOT_TOKEN not set", file=sys.stderr); sys.exit(2)
        if not BACKEND_TOKEN:
            print("error: CLAUDE_TERMINAL_TOKEN not set", file=sys.stderr); sys.exit(2)
        if not self.allowed:
            print("error: no allowed chat_ids — add a user with telegram_chat_id or set BRIDGE_ALLOWED_CHAT_IDS", file=sys.stderr); sys.exit(2)
        print(f"bridge: authorized chats: {sorted(self.allowed)}", flush=True)
        await self.connect_ws()
        await asyncio.gather(self.ws_reader_loop(), self.telegram_poll_loop())


if __name__ == "__main__":
    asyncio.run(Bridge().run())
