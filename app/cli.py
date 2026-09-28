"""Command-line helpers for the application.

Run with ``python -m app.cli <command>``. The first command you need is
``create-admin``; tables are created automatically if missing.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db import dispose_engine, get_session_factory, init_db
from app.models import User
from app.services import auth as auth_service


def _prompt_password() -> str:
    """Ask for a password twice and return it."""
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("Passwords do not match.")
    if len(first) < 8:
        raise SystemExit("Password must be at least 8 characters long.")
    return first


async def _create_user(
    *,
    username: str,
    email: str | None,
    password: str | None,
    is_admin: bool,
    allow_existing: bool,
) -> int:
    if not password:
        password = _prompt_password()

    async with get_session_factory()() as db:
        if not allow_existing and await auth_service.count_users(db) > 0:
            print(
                "A user already exists. Re-run with --allow-existing to add another.",
                file=sys.stderr,
            )
            return 1

        try:
            user = await auth_service.create_user(
                db,
                username=username,
                password=password,
                email=email,
                is_admin=is_admin,
            )
        except auth_service.UserAlreadyExistsError as exc:
            print(f"Could not create user: {exc}", file=sys.stderr)
            return 1

        role = "admin" if user.is_admin else "user"
        print(f"Created {role} user {user.username!r} (id={user.id}).")
        return 0


async def _list_users() -> int:
    async with get_session_factory()() as db:
        result = await db.execute(select(User).order_by(User.username))
        users = list(result.scalars())
        if not users:
            print("No users found.")
            return 0
        for user in users:
            role = "admin" if user.is_admin else "user"
            state = "active" if user.is_active else "disabled"
            email = user.email or "-"
            print(f"{user.id:>4}  {user.username:<24} {role:<6} {state:<8} {email}")
    return 0


async def _purge_sessions() -> int:
    async with get_session_factory()() as db:
        removed = await auth_service.delete_expired_sessions(db)
    print(f"Removed {removed} expired session(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="ia-trading-broker command-line tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_user_arguments(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--username", required=True, help="login name")
        sub.add_argument("--email", default=None, help="optional email address")
        sub.add_argument(
            "--password",
            default=None,
            help="password (prompted for when omitted; prefer the prompt)",
        )
        sub.add_argument(
            "--allow-existing",
            action="store_true",
            help="create another user even when users already exist",
        )

    create_admin = subparsers.add_parser(
        "create-admin", help="create the first administrator account"
    )
    add_user_arguments(create_admin)

    create_user = subparsers.add_parser("create-user", help="create a regular user")
    add_user_arguments(create_user)

    subparsers.add_parser("list-users", help="list application users")
    subparsers.add_parser("purge-sessions", help="delete expired sessions")

    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "create-admin":
        return await _create_user(
            username=args.username,
            email=args.email,
            password=args.password,
            is_admin=True,
            allow_existing=args.allow_existing,
        )
    if args.command == "create-user":
        return await _create_user(
            username=args.username,
            email=args.email,
            password=args.password,
            is_admin=False,
            allow_existing=args.allow_existing,
        )
    if args.command == "list-users":
        return await _list_users()
    if args.command == "purge-sessions":
        return await _purge_sessions()
    raise SystemExit(f"Unknown command: {args.command}")


async def _main_async(args: argparse.Namespace) -> int:
    try:
        await init_db()
        return await _run(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``python -m app.cli``."""
    args = build_parser().parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

