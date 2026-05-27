"""
Claude Terminal — PTY WebSocket Server

Proper PTY integration: Python forks a pseudo-terminal, runs claude in it,
and streams I/O over WebSocket to the browser. No tmux polling. Real-time.
"""
import asyncio
import json
import os
import pty
import struct
import fcntl
import termios
import signal
import select
import logging
import uuid
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("claude-terminal")

# --- Config ---
AUTH_TOKEN = os.environ.get("CLAUDE_TERMINAL_TOKEN", str(uuid.uuid4()))
WORK_DIR = Path(os.environ.get("WORK_DIR", str(Path.home() / "app")))
WORK_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Claude Terminal")

# Active PTY sessions
sessions: dict[str, dict] = {}

class PTYSession:
    """Manages a single PTY with claude running inside it."""
    
    def __init__(self, sid: str):
        self.sid = sid
        self.fd: int | None = None
        self.pid: int | None = None
        self.websocket: WebSocket | None = None
        self._reader_task: asyncio.Task | None = None
        self._buffer = bytearray()

    def spawn(self):
        """Fork a PTY and exec claude inside it."""
        pid, fd = pty.fork()
        if pid == 0:
            # Child process
            os.chdir(str(WORK_DIR))
            
            # Determine claude binary
            claude_cmd = "claude"
            for candidate in ["claude", "npx"]:
                if os.system(f"which {candidate} >/dev/null 2>&1") == 0:
                    break
            
            if claude_cmd == "npx":
                os.execvp("npx", ["npx", "@anthropic-ai/claude-code"])
            else:
                os.execvp(claude_cmd, [claude_cmd])
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

    def write(self, data: str | bytes):
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

    def read(self) -> bytes:
        """Read available data from PTY. Non-blocking."""
        if self.fd is None:
            return b""
        try:
            return os.read(self.fd, 65536)
        except (BlockingIOError, OSError):
            return b""

    async def reader_loop(self):
        """Background loop: read PTY → send to WebSocket."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                # Wait for PTY to have data
                r, _, _ = select.select([self.fd], [], [], 0.05)
                if r:
                    data = self.read()
                    if data:
                        await self._send_terminal_output(data)
                else:
                    await asyncio.sleep(0.01)
            except Exception as e:
                log.error(f"Reader loop error for {self.sid}: {e}")
                break

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
    return {"status": "ok", "sessions": list(sessions.keys())}

@app.get("/api/config")
async def get_config():
    return {
        "token": AUTH_TOKEN,
        "ws_url": "/ws/{session_id}",
        "default_session": "claude-main"
    }

@app.get("/api/sessions")
async def list_sessions():
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

@app.get("/")
@app.get("/{path:path}")
async def serve_frontend():
    frontend_path = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend_path.exists():
        return FileResponse(str(frontend_path))
    return {"message": "Frontend not built yet"}
