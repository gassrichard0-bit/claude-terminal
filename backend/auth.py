"""User accounts + Telegram-based 2FA.

Data file layout (~/.claude-terminal-users.json):

    {
      "admin": {
        "telegram_chat_id": "7275604066",
        "telegram_bot_token": "optional — overrides env"
      },
      "users": {
        "dan": {
          "password_hash": "<salt>$<sha256_hex>",
          "telegram_chat_id": "1234567"
        }
      }
    }

Password storage: sha256(salt + plaintext) with a per-user 16-byte hex salt.
This isn't bcrypt — but with the Telegram OTP layer on top, brute-force from
a leaked file is impractical for this personal-tool scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_USERS_PATH = Path(os.environ.get(
    "CLAUDE_TERMINAL_USERS_FILE",
    str(Path.home() / ".claude-terminal-users.json"),
))


@dataclass
class Admin:
    telegram_chat_id: Optional[str] = None
    telegram_bot_token: Optional[str] = None


@dataclass
class User:
    username: str
    password_hash: str
    telegram_chat_id: Optional[str] = None
    server_url: str = ""
    phone: Optional[str] = None  # E.164, e.g., +14176308774 — used for iMessage OTP


@dataclass
class UserDB:
    admin: Admin
    users: dict[str, User]

    @classmethod
    def load(cls, path: Path = DEFAULT_USERS_PATH) -> "UserDB":
        if not path.exists():
            return cls(admin=Admin(), users={})
        data = json.loads(path.read_text())
        admin_data = data.get("admin") or {}
        users_data = data.get("users") or {}
        return cls(
            admin=Admin(
                telegram_chat_id=admin_data.get("telegram_chat_id"),
                telegram_bot_token=admin_data.get("telegram_bot_token"),
            ),
            users={
                name: User(
                    username=name,
                    password_hash=u.get("password_hash", ""),
                    telegram_chat_id=u.get("telegram_chat_id"),
                    server_url=u.get("server_url", ""),
                    phone=u.get("phone"),
                )
                for name, u in users_data.items()
            },
        )

    def save(self, path: Path = DEFAULT_USERS_PATH) -> None:
        data = {
            "admin": {
                "telegram_chat_id": self.admin.telegram_chat_id,
                "telegram_bot_token": self.admin.telegram_bot_token,
            },
            "users": {
                name: {
                    "password_hash": u.password_hash,
                    "telegram_chat_id": u.telegram_chat_id,
                }
                for name, u in self.users.items()
            },
        }
        path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def hash_password(plain: str, salt: Optional[str] = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(plain: str, stored: str) -> bool:
    if "$" not in stored:
        return False
    salt, _digest = stored.split("$", 1)
    return secrets.compare_digest(stored, hash_password(plain, salt))


def generate_otp() -> str:
    """6-digit numeric code, zero-padded."""
    return f"{secrets.randbelow(1_000_000):06d}"


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: float = 5.0) -> bool:
    """POST to Telegram sendMessage. Returns True on 200 OK. Kept for the
    Telegram bridge daemon; no longer used by the auth OTP flow."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


_PHONE_OK = re.compile(r"^\+?[0-9]{7,15}$")


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Strip formatting; reject anything that isn't digits/+. 10-digit numbers
    are assumed US (+1)."""
    if not phone:
        return None
    cleaned = re.sub(r"[\s\-().]", "", phone.strip())
    if not _PHONE_OK.match(cleaned):
        return None
    if not cleaned.startswith("+") and len(cleaned) == 10:
        cleaned = "+1" + cleaned
    elif not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def _applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\"", "\\\"")


def send_imessage(phone: Optional[str], text: str, timeout: float = 10.0) -> bool:
    """Send `text` to `phone` via macOS Messages.app (iMessage or SMS via
    Continuity). Returns True on success.

    No external service / API key — needs Messages.app signed in, and for SMS
    fallback an iPhone with Text Message Forwarding enabled."""
    target = _normalize_phone(phone)
    if not target:
        return False
    safe_text = _applescript_escape(text)
    script = (
        'tell application "Messages"\n'
        '  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{target}" of targetService\n'
        f'  send "{safe_text}" to targetBuddy\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(address: Optional[str], subject: str, body: str, timeout: float = 15.0) -> bool:
    """Send via macOS Mail.app. Requires Mail.app signed in with an account."""
    if not address or not _EMAIL_OK.match(address.strip()):
        return False
    addr = address.strip()
    safe_subject = _applescript_escape(subject)
    safe_body = _applescript_escape(body)
    safe_addr = _applescript_escape(addr)
    script = (
        'tell application "Mail"\n'
        '  set newMsg to make new outgoing message with properties '
        f'{{subject:"{safe_subject}", content:"{safe_body}", visible:false}}\n'
        '  tell newMsg\n'
        f'    make new to recipient with properties {{address:"{safe_addr}"}}\n'
        '  end tell\n'
        '  send newMsg\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


@dataclass
class Challenge:
    username: str
    code: str
    expires_at: float
    attempts: int = 0


CHALLENGE_TTL_SECONDS = 5 * 60
MAX_CHALLENGE_ATTEMPTS = 5


def new_challenge(username: str) -> tuple[str, Challenge]:
    cid = secrets.token_urlsafe(24)
    return cid, Challenge(
        username=username,
        code=generate_otp(),
        expires_at=time.time() + CHALLENGE_TTL_SECONDS,
    )
