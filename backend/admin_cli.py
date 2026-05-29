#!/usr/bin/env python3
"""Manage Claude Terminal user accounts.

Usage:
    python3 -m backend.admin_cli init --admin-chat-id 7275604066
    python3 -m backend.admin_cli adduser dan --chat-id 1234567
    python3 -m backend.admin_cli list
    python3 -m backend.admin_cli deluser dan
    python3 -m backend.admin_cli reset-password dan
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from backend.auth import UserDB, Admin, User, hash_password, DEFAULT_USERS_PATH


def cmd_init(args):
    db = UserDB.load(args.path)
    db.admin = Admin(
        telegram_chat_id=args.admin_chat_id,
        telegram_bot_token=args.bot_token,
    )
    db.save(args.path)
    print(f"Initialized {args.path}")
    print(f"  admin telegram_chat_id: {db.admin.telegram_chat_id}")
    print(f"  bot token override:     {'set' if db.admin.telegram_bot_token else 'using env TELEGRAM_BOT_TOKEN'}")


def cmd_adduser(args):
    db = UserDB.load(args.path)
    if args.username in db.users and not args.replace:
        print(f"User {args.username!r} already exists. Pass --replace to overwrite.", file=sys.stderr)
        sys.exit(2)
    if args.password:
        pw = args.password
    else:
        pw = getpass.getpass(f"Password for {args.username}: ")
        confirm = getpass.getpass("Confirm: ")
        if pw != confirm:
            print("Passwords don't match.", file=sys.stderr)
            sys.exit(2)
    db.users[args.username] = User(
        username=args.username,
        password_hash=hash_password(pw),
        telegram_chat_id=args.chat_id,
    )
    db.save(args.path)
    print(f"Added user {args.username!r} (telegram_chat_id={args.chat_id or 'none'})")


def cmd_list(args):
    db = UserDB.load(args.path)
    print(f"admin chat_id: {db.admin.telegram_chat_id or '<unset>'}")
    print(f"users: {len(db.users)}")
    for name, u in db.users.items():
        print(f"  - {name:<20} chat_id={u.telegram_chat_id or '<unset>'}")


def cmd_deluser(args):
    db = UserDB.load(args.path)
    if db.users.pop(args.username, None):
        db.save(args.path)
        print(f"Removed {args.username!r}")
    else:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        sys.exit(2)


def cmd_reset_password(args):
    db = UserDB.load(args.path)
    user = db.users.get(args.username)
    if not user:
        print(f"No such user: {args.username!r}", file=sys.stderr)
        sys.exit(2)
    pw = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm: ")
    if pw != confirm:
        print("Passwords don't match.", file=sys.stderr)
        sys.exit(2)
    user.password_hash = hash_password(pw)
    db.save(args.path)
    print(f"Password reset for {args.username!r}")


def main():
    p = argparse.ArgumentParser(prog="admin_cli", description="Claude Terminal user management")
    p.add_argument("--path", type=Path, default=DEFAULT_USERS_PATH,
                   help=f"users file (default: {DEFAULT_USERS_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Set admin chat_id + optional bot token")
    s.add_argument("--admin-chat-id", required=True)
    s.add_argument("--bot-token", help="(optional) overrides TELEGRAM_BOT_TOKEN env")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("adduser", help="Add a user")
    s.add_argument("username")
    s.add_argument("--chat-id", help="Telegram chat_id for OTP delivery")
    s.add_argument("--password", help="(optional) inline; prompts if omitted")
    s.add_argument("--replace", action="store_true")
    s.set_defaults(func=cmd_adduser)

    s = sub.add_parser("list", help="List users")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("deluser", help="Remove a user")
    s.add_argument("username")
    s.set_defaults(func=cmd_deluser)

    s = sub.add_parser("reset-password", help="Change a user's password")
    s.add_argument("username")
    s.set_defaults(func=cmd_reset_password)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
