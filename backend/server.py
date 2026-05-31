"""
Claude Terminal — PTY WebSocket Server

Proper PTY integration: Python forks a pseudo-terminal, runs claude in it,
and streams I/O over WebSocket to the browser. No tmux polling. Real-time.
"""
import asyncio
import json
import os
import time
import pty
import struct
import fcntl
import termios
import signal
import select
import logging
import uuid
from typing import Optional, Union
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from pydantic import BaseModel as _PydanticBaseModel
from backend.auth import (
    UserDB, Challenge, hash_password, verify_password,
    new_challenge, send_telegram, send_imessage, send_email,
    CHALLENGE_TTL_SECONDS, MAX_CHALLENGE_ATTEMPTS,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("claude-terminal")

# --- Config ---
def _load_or_create_auth_token() -> str:
    """Resolution: CLAUDE_TERMINAL_TOKEN env > ~/.claude-terminal-token >
    freshly generated. Generated tokens are 32-byte url-safe; file is 0600."""
    import secrets as _secrets
    env_token = os.environ.get("CLAUDE_TERMINAL_TOKEN", "").strip()
    if env_token:
        return env_token
    token_path = Path.home() / ".claude-terminal-token"
    if token_path.exists():
        existing = token_path.read_text().strip()
        if existing:
            return existing
    token = _secrets.token_urlsafe(32)
    token_path.write_text(token)
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return token

AUTH_TOKEN = _load_or_create_auth_token()
WORK_DIR = Path(os.environ.get("WORK_DIR", str(Path.home() / "app")))
WORK_DIR.mkdir(parents=True, exist_ok=True)

# LAN fallback URL advertised to clients. Used when the cloud tunnel is
# unreachable but client and server are on the same wifi.
import socket as _socket
def _default_lan_url() -> str:
    host = _socket.gethostname()
    # Bonjour name on macOS is usually <name>.local. gethostname() may already
    # return that form or just the bare name.
    if host and "." not in host:
        host = host + ".local"
    port = os.environ.get("PORT", "8080")
    return f"http://{host}:{port}"

LAN_URL = os.environ.get("CLAUDE_TERMINAL_LAN_URL") or _default_lan_url()

# Email fallback for OTP delivery when a user's iMessage send fails (e.g.,
# Messages.app not signed in, recipient not iMessage, AppleScript permission
# not yet granted).
ADMIN_EMAIL = os.environ.get("CLAUDE_TERMINAL_ADMIN_EMAIL", "gassrichard@gmail.com")

# CORS origins — comma-separated list via env, defaults to '*' for backwards
# compatibility but production setups should pin to specific origins.
_cors_origins_env = os.environ.get("CLAUDE_TERMINAL_CORS_ORIGINS", "*")
CORS_ORIGINS = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]

print("=" * 60, flush=True)
print("  Claude Terminal — Auth Token", flush=True)
print(f"  {AUTH_TOKEN}", flush=True)
print(f"  (persisted at ~/.claude-terminal-token; LAN: {LAN_URL})", flush=True)
print("=" * 60, flush=True)

app = FastAPI(title="Claude Terminal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- User accounts + 2FA ---
USER_DB = UserDB.load()
_pending: dict[str, Challenge] = {}
TELEGRAM_BOT_TOKEN = (
    USER_DB.admin.telegram_bot_token
    or os.environ.get("TELEGRAM_BOT_TOKEN", "")
)

# Per-session tokens issued on successful 2FA verify (or password-only login
# for users without a Telegram chat id). Survives only while the process is
# alive — restart invalidates all sessions (forced re-login).
SESSION_TTL_SECONDS = int(os.environ.get("CLAUDE_TERMINAL_SESSION_TTL", str(30 * 24 * 3600)))


from dataclasses import dataclass as _dc
@_dc
class _AuthSession:
    username: str
    created_at: float
    expires_at: float

# Maps session_token -> _AuthSession. AUTH_TOKEN (the system token) is
# accepted independently — it's not stored here.
SESSIONS: dict[str, _AuthSession] = {}


def _mint_session(username: str) -> str:
    import secrets as _s
    token = _s.token_urlsafe(32)
    now = time.time()
    SESSIONS[token] = _AuthSession(
        username=username,
        created_at=now,
        expires_at=now + SESSION_TTL_SECONDS,
    )
    return token


def _gc_sessions():
    """Drop expired session tokens. O(n) — fine for the size of this app."""
    now = time.time()
    dead = [t for t, s in SESSIONS.items() if s.expires_at < now]
    for t in dead:
        SESSIONS.pop(t, None)


def _validate_token(token: str) -> bool:
    """True if `token` is the system AUTH_TOKEN or an unexpired session token."""
    if not token:
        return False
    if secrets_compare(token, AUTH_TOKEN):
        return True
    sess = SESSIONS.get(token)
    if sess is None:
        return False
    if sess.expires_at < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


class LoginRequest(_PydanticBaseModel):
    username: str
    password: str


class VerifyRequest(_PydanticBaseModel):
    challenge_id: str
    code: str


def _gc_pending():
    """Sweep expired challenges. Cheap enough to run on every login."""
    now = time.time()
    expired = [cid for cid, ch in _pending.items() if ch.expires_at < now]
    for cid in expired:
        _pending.pop(cid, None)


def secrets_compare(a: str, b: str) -> bool:
    import secrets as _s
    return _s.compare_digest(a, b)


def require_token(request: Request):
    """Check the request carries the auth token.

    Accepts either a `?token=` query param or an `Authorization: Bearer <t>`
    header. Returns the token on success, raises HTTPException(401) on
    mismatch. Use as `Depends(require_token)` on any endpoint that exposes
    PTY control or conversation data."""
    token = request.query_params.get("token", "")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not _validate_token(token):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return token

# Active PTY sessions
sessions: dict[str, "PTYSession"] = {}

# Cache for /api/messages — {mtime, messages}
_messages_cache = None

class PTYSession:
    """Manages a single PTY with claude running inside it."""
    
    def __init__(self, sid: str):
        self.sid = sid
        self.fd: Optional[int] = None
        self.pid: Optional[int] = None
        # Fan-out: PTY output goes to every attached websocket; input from
        # any attached websocket goes to the same PTY. Lets the PWA and the
        # Telegram bridge share one Claude session.
        self.websockets: list[WebSocket] = []
        self._reader_task: Optional[asyncio.Task] = None
        self._buffer = bytearray()

    def spawn(self, startup: Optional[str] = None, raw_mode: bool = True):
        """Fork a PTY. Default startup auto-resumes the most recent Claude
        session via `claude --continue` and falls back to a fresh `claude`,
        dropping to bash if claude exits. Callers can pass a custom `startup`
        shell string (e.g. for an agent bash) to run a different CLI.

        raw_mode controls PTY discipline:
        - True (Claude): ECHO/ICANON/ISIG/IEXTEN off, OPOST off — full TUI
          owns its own rendering, raw byte passthrough.
        - False (agent bash): leave cooked defaults so bash echoes typed
          chars, line-edits via readline, and `\\n` is translated to `\\r\\n`
          for the prompt. Without this, an interactive shell looks broken."""
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(WORK_DIR))
            # Tell apps we support 256-color and force color output for CLIs that opt-in
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            os.environ["FORCE_COLOR"] = "1"
            os.environ["CLICOLOR_FORCE"] = "1"
            cmd = startup or "claude --continue 2>/dev/null || claude; exec bash --login"
            os.execvp("bash", ["bash", "--login", "-c", cmd])
            os._exit(1)

        self.pid = pid
        self.fd = fd

        if raw_mode:
            attrs = termios.tcgetattr(fd)
            attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
            attrs[1] = attrs[1] & ~termios.OPOST
            termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)

        # Set to non-blocking
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        log.info(f"PTY spawned: pid={pid}, fd={fd}, raw={raw_mode}")

    def resize(self, cols: int, rows: int):
        """Set terminal window size."""
        if self.fd:
            size = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)

    def write(self, data: Union[str, bytes]):
        """Write data to the PTY (into claude's stdin)."""
        if self.fd is None:
            return
        if isinstance(data, str):
            data = data.encode("utf-8")
        os.write(self.fd, data)

    def write_control(self, char: str):
        """Send a control character."""
        ctrl_map = {
            "c": b"\x03",  # Ctrl+C
            "d": b"\x04",  # Ctrl+D
            "z": b"\x1a",  # Ctrl+Z
            "l": b"\x0c",  # Ctrl+L (clear)
        }
        if char in ctrl_map:
            self.write(ctrl_map[char])

    def read(self) -> Optional[bytes]:
        """Read available data from PTY (non-blocking).

        Returns bytes when data is available, b"" when the fd is readable but
        nothing's ready (rare with O_NONBLOCK + add_reader), and None on EOF
        or fd error — callers MUST stop reading after None or they'll spin."""
        if self.fd is None:
            return None
        try:
            data = os.read(self.fd, 65536)
            return data if data else None  # zero-length read = EOF
        except BlockingIOError:
            return b""
        except OSError:
            return None  # EIO when slave hangs up, EBADF if fd was closed

    async def reader_loop(self):
        """Background loop: read PTY → send to WebSocket.

        Uses the asyncio event loop's add_reader (file-descriptor readiness
        notification) instead of select() polling, so there's no idle delay
        between Claude emitting bytes and our WS forwarding them. Reads are
        also coalesced into a single WS frame when multiple chunks land in
        the same 5ms window — this halves protocol overhead during streaming
        bursts without adding meaningful latency.

        Bulletproofing:
        - Captures fd locally so concurrent cleanup() can't trip remove_reader.
        - Detects EOF (child exited / pty closed) and breaks instead of
          busy-spinning on a permanently-readable closed fd.
        - Caps per-iteration buffer at 1 MiB so a runaway producer can't
          balloon memory before we send.
        - Lets CancelledError propagate so cleanup().cancel() works."""
        loop = asyncio.get_event_loop()
        data_ready = asyncio.Event()
        fd = self.fd
        if fd is None:
            return
        MAX_COALESCE = 1 << 20  # 1 MiB
        try:
            loop.add_reader(fd, data_ready.set)
        except Exception as e:
            log.error(f"add_reader failed for {self.sid}: {e}")
            return
        try:
            while True:
                await data_ready.wait()
                data_ready.clear()
                buf = bytearray()
                eof = False
                # Drain everything immediately available
                while len(buf) < MAX_COALESCE:
                    chunk = self.read()
                    if chunk is None:
                        eof = True
                        break
                    if not chunk:
                        break
                    buf.extend(chunk)
                # Coalesce any follow-up chunks that arrive within 5 ms — this
                # collapses Claude's tight streaming bursts into one WS frame.
                if not eof and len(buf) < MAX_COALESCE:
                    try:
                        await asyncio.wait_for(data_ready.wait(), timeout=0.005)
                        data_ready.clear()
                        while len(buf) < MAX_COALESCE:
                            chunk = self.read()
                            if chunk is None:
                                eof = True
                                break
                            if not chunk:
                                break
                            buf.extend(chunk)
                    except asyncio.TimeoutError:
                        pass
                if buf:
                    await self._send_terminal_output(bytes(buf))
                if eof:
                    log.info(f"Reader loop EOF for {self.sid}")
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Reader loop error for {self.sid}: {e}")
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    async def _send_terminal_output(self, data: bytes):
        """Fan PTY output to every attached websocket. Dead sockets are
        pruned from the list on send failure."""
        if not self.websockets:
            return
        text = data.decode("utf-8", errors="replace")
        dead = []
        for ws in list(self.websockets):
            try:
                await ws.send_json({"type": "output", "data": text})
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.websockets.remove(ws)
            except ValueError:
                pass

    def attach_websocket(self, ws: WebSocket):
        if ws not in self.websockets:
            self.websockets.append(ws)

    def detach_websocket(self, ws: Optional[WebSocket] = None):
        """Remove one specific ws, or clear all if not given."""
        if ws is None:
            self.websockets.clear()
        else:
            try:
                self.websockets.remove(ws)
            except ValueError:
                pass

    def cleanup(self):
        """Kill the child process and close the PTY."""
        self.detach_websocket()
        if self._reader_task:
            self._reader_task.cancel()
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
        if self.fd:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.pid = None
        self.fd = None

    async def start_reader(self):
        self._reader_task = asyncio.create_task(self.reader_loop())


# --- API Routes ---

@app.get("/api/health")
async def health():
    # Public — used by the PWA setup screen to verify a backend URL,
    # and to learn the LAN fallback URL for offline-tunnel resilience.
    return {"status": "ok", "lan_url": LAN_URL}


@app.get("/api/auth/configured")
async def auth_configured():
    """Public — frontend hits this to know whether to show the login screen
    (users defined) or fall back to the simple URL+token setup."""
    return {
        "enabled": bool(USER_DB.users),
        "admin_alerts": bool(USER_DB.admin.telegram_chat_id),
        "lan_url": LAN_URL,
    }


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest, request: Request):
    """Step 1 of 2FA: validate username+password, send OTP via Telegram to
    the user and (separately) to the admin as a login alert. Returns a
    challenge_id which step 2 verifies."""
    _gc_pending()
    user = USER_DB.users.get(req.username)
    # Use compare_digest-style verify even when user is missing so that
    # response timing doesn't leak username existence.
    dummy = "salt$" + "0" * 64
    valid = user is not None and verify_password(req.password, user.password_hash)
    if user is None:
        verify_password(req.password, dummy)
    if not valid:
        log.warning(f"auth: failed login for username={req.username!r} from {request.client.host if request.client else '?'}")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    src_ip = request.client.host if request.client else "?"

    # No phone configured for this user — skip 2FA, issue session token.
    # Email-only fallback is only used as a backup to iMessage; without a
    # phone there's nothing to fall back from.
    if not user.phone:
        _gc_sessions()
        sess_token = _mint_session(user.username)
        log.info(f"auth: password-only login for {user.username} from {src_ip}")
        return {
            "token": sess_token,
            "username": user.username,
            "server_url": user.server_url or "",
            "skip_2fa": True,
        }

    cid, challenge = new_challenge(user.username)
    _pending[cid] = challenge

    sms_text = (
        f"Claude Terminal — sign-in code {challenge.code} "
        f"(user: {user.username}, from {src_ip}). Expires in 5 min."
    )
    delivered_via = None
    if user.phone and send_imessage(user.phone, sms_text):
        delivered_via = f"iMessage→{user.phone}"
    elif send_email(
        ADMIN_EMAIL,
        "Claude Terminal sign-in code",
        f"{sms_text}\n\nIf this wasn't you, ignore this email and rotate your password.",
    ):
        delivered_via = f"email→{ADMIN_EMAIL}"

    if not delivered_via:
        _pending.pop(cid, None)
        log.error(f"auth: both iMessage and email delivery failed for {user.username}")
        raise HTTPException(status_code=503, detail="Could not deliver verification code")

    log.info(f"auth: challenge issued for {user.username} from {src_ip} via {delivered_via}")
    return {"challenge_id": cid, "expires_in": CHALLENGE_TTL_SECONDS}


@app.post("/api/auth/verify")
async def auth_verify(req: VerifyRequest, request: Request):
    """Step 2 of 2FA: validate the 6-digit code. On success, return the
    backend AUTH_TOKEN so the PWA can connect."""
    _gc_pending()
    ch = _pending.get(req.challenge_id)
    if ch is None:
        raise HTTPException(status_code=400, detail="Challenge expired or not found")
    if ch.attempts >= MAX_CHALLENGE_ATTEMPTS:
        _pending.pop(req.challenge_id, None)
        raise HTTPException(status_code=429, detail="Too many attempts; restart login")
    ch.attempts += 1
    if not secrets_compare(req.code.strip(), ch.code):
        log.warning(f"auth: bad code attempt {ch.attempts}/{MAX_CHALLENGE_ATTEMPTS} for {ch.username}")
        raise HTTPException(status_code=401, detail="Invalid code")
    # Success — consume the challenge and mint a per-session token
    _pending.pop(req.challenge_id, None)
    _gc_sessions()
    sess_token = _mint_session(ch.username)
    log.info(f"auth: {ch.username} verified")
    return {"token": sess_token, "username": ch.username}

@app.get("/api/config")
async def get_config(_: str = Depends(require_token)):
    # Token gated: caller already proved they have the token, so this
    # returns layout details only (no secrets).
    return {
        "ws_url": "/ws/{session_id}",
        "default_session": "claude-main"
    }

def _slice_messages(all_msgs, since: int, limit: int, last: int):
    """Apply pagination params and return (sliced, start_index)."""
    total = len(all_msgs)
    if last > 0:
        start = max(0, total - last)
        return all_msgs[start:], start
    start = max(0, since)
    sliced = all_msgs[start:]
    if limit > 0:
        sliced = sliced[:limit]
    return sliced, start

def _parse_jsonl(path: Path):
    """Yield (role, content, timestamp) tuples from one Claude session JSONL."""
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role") or entry.get("type") or ""
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    pieces = []
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") == "text":
                                pieces.append(part.get("text", ""))
                        elif isinstance(part, str):
                            pieces.append(part)
                    content = "\n".join(p for p in pieces if p).strip()
                elif isinstance(content, str):
                    content = content.strip()
                else:
                    content = ""
                if not content:
                    continue
                yield {
                    "role": role,
                    "content": content,
                    "timestamp": entry.get("timestamp", ""),
                }
    except OSError:
        return

@app.get("/api/messages")
async def get_messages(since: int = 0, limit: int = 0, last: int = 0, _: str = Depends(require_token)):
    """Return chat messages merged across all Claude project sessions.

    Reads every *.jsonl under ~/.claude/projects/*/ and stitches them into
    one chronological stream sorted by timestamp. Cache key is (file count,
    max mtime) so any session append / new session reparses.

    ?since=<N>     skip first N messages (delta polling)
    ?limit=<M>     return at most M messages from the slice (paginated chunks)
    ?last=<N>      return only the last N messages (initial load)
    Response includes 'total' (full count) and 'start_index' (absolute index
    of the first returned message), so the client can paginate correctly.
    """
    global _messages_cache
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return {"messages": [], "session_file": None, "total": 0, "start_index": 0}

    jsonl_files = list(projects_dir.glob("*/*.jsonl"))
    if not jsonl_files:
        return {"messages": [], "session_file": None, "total": 0, "start_index": 0}

    max_mtime = max(p.stat().st_mtime for p in jsonl_files)
    fingerprint = (len(jsonl_files), max_mtime)
    session_token = "all-projects"

    if _messages_cache is not None and _messages_cache.get("fingerprint") == fingerprint:
        all_msgs = _messages_cache["messages"]
        msgs, start = _slice_messages(all_msgs, since, limit, last)
        return {
            "messages": msgs,
            "session_file": session_token,
            "total": len(all_msgs),
            "start_index": start,
            "cached": True,
        }

    # Parse every session file, then sort chronologically. Entries with no
    # timestamp sink to the end of their file's insertion order via the
    # secondary sort keys (file mtime, line index).
    merged = []
    for path in jsonl_files:
        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            continue
        for line_idx, m in enumerate(_parse_jsonl(path)):
            merged.append((m["timestamp"] or "", file_mtime, line_idx, m))
    merged.sort(key=lambda x: (x[0], x[1], x[2]))
    out = [m for *_x, m in merged]

    _messages_cache = {"fingerprint": fingerprint, "messages": out, "session_file": session_token}
    msgs, start = _slice_messages(out, since, limit, last)

    return {
        "messages": msgs,
        "session_file": session_token,
        "total": len(out),
        "start_index": start,
        "cached": False,
    }

@app.get("/api/sessions")
async def list_sessions(_: str = Depends(require_token)):
    return {"sessions": [
        {"id": sid, "alive": s.pid is not None}
        for sid, s in sessions.items()
    ]}


# --- WebSocket ---

# Agent (raw shell) endpoint — declared BEFORE /ws/{session_id} so it wins
# the route match. Starlette dispatches in registration order; if /ws/agent
# were declared after the parametric route it would never be hit.
# Plain bash login shell; the user runs whatever agent they want inside it
# (hermes, claude, codex…). Independent PTY from the Claude session.
AGENT_STARTUP = "exec bash --login"


@app.websocket("/ws/agent")
async def agent_websocket(websocket: WebSocket):
    """Live agent PTY session — a plain bash shell."""
    await websocket.accept()
    token = websocket.query_params.get("token", "")
    if not _validate_token(token):
        log.warning(f"WS agent rejected: bad token from {websocket.client.host if websocket.client else '?'}")
        await websocket.close(code=4001, reason="Invalid token")
        return
    log.info("WS connected: agent session")

    session_id = "agent-main"
    session = sessions.get(session_id)
    fresh = False
    if session is None or session.pid is None:
        log.info("Creating new agent PTY session (bash)")
        session = PTYSession(session_id)
        session.spawn(startup=AGENT_STARTUP, raw_mode=False)
        sessions[session_id] = session
        await session.start_reader()
        fresh = True

    session.attach_websocket(websocket)
    session.resize(80, 24)
    # Nudge bash to repaint its prompt on every (re)connect — without this a
    # reconnecting client lands on a blank screen until they hit Enter, since
    # bash's PS1 is only emitted in response to input.
    if not fresh:
        try:
            session.write("\r")
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                session.write(data)
                continue

            msg_type = msg.get("type", "")
            if msg_type == "input":
                session.write(msg.get("data", ""))
            elif msg_type == "enter":
                session.write("\r")
            elif msg_type == "resize":
                session.resize(msg.get("cols", 80), msg.get("rows", 24))
            elif msg_type == "control":
                session.write_control(msg.get("char", ""))
            elif msg_type == "restart":
                log.info("Restarting agent session")
                session.cleanup()
                new_session = PTYSession(session_id)
                new_session.spawn(startup=AGENT_STARTUP, raw_mode=False)
                new_session.attach_websocket(websocket)
                sessions[session_id] = new_session
                await new_session.start_reader()

    except WebSocketDisconnect:
        log.info("WS disconnected: agent session")
    except Exception as e:
        log.error(f"WS agent error: {e}")
    finally:
        if session_id in sessions:
            sessions[session_id].detach_websocket(websocket)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """Live terminal session over WebSocket."""
    # Accept FIRST so the 4001 close code actually reaches the browser.
    # Closing before accept logs as plain HTTP 403 and the client never
    # learns to re-prompt — it just retries forever.
    await websocket.accept()
    token = websocket.query_params.get("token", "")
    if not _validate_token(token):
        log.warning(f"WS rejected: bad token from {websocket.client.host if websocket.client else '?'}")
        await websocket.close(code=4001, reason="Invalid token")
        return
    log.info(f"WS connected: session={session_id}")

    # Get or create PTY session
    session = sessions.get(session_id)
    if session is None:
        log.info(f"Creating new PTY session: {session_id}")
        session = PTYSession(session_id)
        session.spawn()
        sessions[session_id] = session
        await session.start_reader()

    session.attach_websocket(websocket)
    session.resize(80, 24)

    try:
        while True:
            data = await websocket.receive_text()
            
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                session.write(data)
                continue

            msg_type = msg.get("type", "")
            
            if msg_type == "input":
                session.write(msg.get("data", ""))
            
            elif msg_type == "enter":
                session.write("\r")
            
            elif msg_type == "resize":
                session.resize(
                    msg.get("cols", 80),
                    msg.get("rows", 24)
                )
            
            elif msg_type == "control":
                session.write_control(msg.get("char", ""))
            
            elif msg_type == "restart":
                log.info(f"Restarting session: {session_id}")
                session.cleanup()
                new_session = PTYSession(session_id)
                new_session.spawn()
                new_session.attach_websocket(websocket)
                sessions[session_id] = new_session
                await new_session.start_reader()

    except WebSocketDisconnect:
        log.info(f"WS disconnected: session={session_id}")
    except Exception as e:
        log.error(f"WS error for {session_id}: {e}")
    finally:
        if session_id in sessions:
            # Only remove THIS websocket — others (PWA, bridge) keep streaming
            sessions[session_id].detach_websocket(websocket)
            # Keep PTY alive — users can reconnect


# --- Frontend ---

# Serve xterm static files
app.mount("/xterm", StaticFiles(directory=str(FRONTEND_DIR / "xterm")), name="xterm")

@app.get("/")
async def serve_frontend():
    frontend_path = FRONTEND_DIR / "index.html"
    if frontend_path.exists():
        return FileResponse(
            str(frontend_path),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return {"message": "Frontend not built yet"}

# Serve any other static files from frontend/. Path-traversal-safe:
# resolved path must remain under FRONTEND_DIR.
@app.get("/files/{path:path}")
async def serve_static(path: str):
    root = FRONTEND_DIR.resolve()
    try:
        file_path = (root / path).resolve()
        file_path.relative_to(root)
    except (ValueError, OSError):
        return HTMLResponse("Forbidden", status_code=403)
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    return HTMLResponse("Not found", status_code=404)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    log.info(f"Starting Claude Terminal on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
