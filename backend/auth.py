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
import secrets
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
    """POST to Telegram sendMessage. Returns True on 200 OK."""
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
