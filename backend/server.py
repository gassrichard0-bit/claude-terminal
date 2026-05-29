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
    new_challenge, send_telegram,
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
AUTH_TOKEN = os.environ.get("CLAUDE_TERMINAL_TOKEN", str(uuid.uuid4()))
WORK_DIR = Path(os.environ.get("WORK_DIR", str(Path.home() / "app")))
WORK_DIR.mkdir(parents=True, exist_ok=True)

# CORS origins — comma-separated list via env, defaults to '*' for backwards
# compatibility but production setups should pin to specific origins.
_cors_origins_env = os.environ.get("CLAUDE_TERMINAL_CORS_ORIGINS", "*")
CORS_ORIGINS = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]

# Print the token at startup so the user can paste it into the PWA setup
# screen out-of-band. Anyone with the ngrok URL must ALSO have this token
# to connect — that's the only protection against random scanners finding
# the public tunnel.
print("=" * 60, flush=True)
print("  Claude Terminal — Auth Token", flush=True)
print(f"  {AUTH_TOKEN}", flush=True)
print("  Paste this into the PWA Setup screen alongside your URL.", flush=True)
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
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return token

# Active PTY sessions
sessions: dict[str, dict] = {}

# Cache for /api/messages — {mtime, messages}
_messages_cache = None

class PTYSession:
    """Manages a single PTY with claude running inside it."""
    
    def __init__(self, sid: str):
        self.sid = sid
        self.fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.websocket: Optional[WebSocket] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._buffer = bytearray()

    def spawn(self):
        """Fork a PTY. Auto-resumes the most recent Claude session via `claude --continue`,
        falls back to a fresh `claude` if no prior session exists, and drops to bash if claude exits."""
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(WORK_DIR))
            # Tell apps we support 256-color and force color output for CLIs that opt-in
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            os.environ["FORCE_COLOR"] = "1"
            os.environ["CLICOLOR_FORCE"] = "1"
            startup = "claude --continue 2>/dev/null || claude; exec bash --login"
            os.execvp("bash", ["bash", "--login", "-c", startup])
            os._exit(1)
        
        self.pid = pid
        self.fd = fd
        
        # Set raw mode on PTY
        attrs = termios.tcgetattr(fd)
        attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        attrs[1] = attrs[1] & ~termios.OPOST
        termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)
        
        # Set to non-blocking
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        
        log.info(f"PTY spawned: pid={pid}, fd={fd}")

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
        """Send terminal output to connected WebSocket."""
        if self.websocket is None:
            return
        try:
            await self.websocket.send_json({
                "type": "output",
                "data": data.decode("utf-8", errors="replace")
            })
        except Exception:
            pass

    def attach_websocket(self, ws: WebSocket):
        self.websocket = ws

    def detach_websocket(self):
        self.websocket = None

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
    # Public — used by the PWA setup screen to verify a backend URL.
    # Intentionally returns minimal info, no session list.
    return {"status": "ok"}


@app.get("/api/auth/configured")
async def auth_configured():
    """Public — frontend hits this to know whether to show the login screen
    (users defined) or fall back to the simple URL+token setup."""
    return {
        "enabled": bool(USER_DB.users),
        "admin_alerts": bool(USER_DB.admin.telegram_chat_id),
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

    cid, challenge = new_challenge(user.username)
    _pending[cid] = challenge

    src_ip = request.client.host if request.client else "?"
    user_msg = (
        f"Claude Terminal — your sign-in code is {challenge.code}\n"
        f"It expires in 5 minutes. Don't share it."
    )
    admin_msg = (
        f"Login attempt: {user.username}\n"
        f"From: {src_ip}\n"
        f"Code: {challenge.code}\n"
        f"(this is an audit notification — do not share)"
    )
    if user.telegram_chat_id:
        send_telegram(TELEGRAM_BOT_TOKEN, user.telegram_chat_id, user_msg)
    if USER_DB.admin.telegram_chat_id:
        send_telegram(TELEGRAM_BOT_TOKEN, USER_DB.admin.telegram_chat_id, admin_msg)
    log.info(f"auth: challenge issued for {user.username} from {src_ip}")
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
    # Success — consume the challenge and hand back the backend token
    _pending.pop(req.challenge_id, None)
    log.info(f"auth: {ch.username} verified")
    return {"token": AUTH_TOKEN, "username": ch.username}

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

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """Live terminal session over WebSocket."""
    token = websocket.query_params.get("token", "")
    if token != AUTH_TOKEN:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
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
            sessions[session_id].detach_websocket()
            # Keep PTY alive — user can reconnect


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
