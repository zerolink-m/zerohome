#!/usr/bin/env python3
"""Operator-only first-admin bootstrap; deliberately not an HTTP endpoint."""
from __future__ import annotations

import asyncio
import getpass
import re
import sys
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.runtime_config import SystemSettings
from app.models.user import User


async def create_first_admin(db: AsyncSession, *, name: str, surname: str,
                             email: str, password: str) -> int:
    """Serialize bootstrap attempts on the singleton settings row."""
    lock = await db.scalar(select(SystemSettings).where(SystemSettings.id == 1).with_for_update())
    if lock is None:
        raise RuntimeError("runtime configuration is not initialized; wait for app startup")
    existing = await db.scalar(select(User.id).where(User.role == "admin").limit(1))
    if existing is not None:
        raise RuntimeError("an administrator already exists; refusing to modify users")
    user = User(name=name, surname=surname, email=email.lower(),
                password=await hash_password(password), role="admin",
                added_at=int(time.time()), updated_at=int(time.time()))
    db.add(user)
    try:
        await db.flush()
        user_id = user.id
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise RuntimeError("user could not be created (email may already be registered)") from exc
    return user_id


def prompt() -> dict[str, str]:
    if not sys.stdin.isatty():
        raise RuntimeError("a TTY is required; run docker compose exec -it app python /app/deploy/bootstrap_admin.py")
    name = input("Admin first name: ").strip()
    surname = input("Admin surname: ").strip()
    email = input("Admin email: ").strip().lower()
    password = getpass.getpass("Admin password (input hidden): ")
    confirm = getpass.getpass("Repeat admin password: ")
    if not name or not surname or len(name) > 64 or len(surname) > 64:
        raise ValueError("name and surname must be 1-64 characters")
    if len(password) < 12 or len(password) > 128 or password != confirm:
        raise ValueError("passwords must match and contain 12-128 characters")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 255:
        raise ValueError("enter a valid email address")
    return {"name": name, "surname": surname, "email": email, "password": password}


async def main_async() -> int:
    values = prompt()
    async with AsyncSessionLocal() as db:
        user_id = await create_first_admin(db, **values)
    print(f"Created first administrator (user id {user_id}).")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except (RuntimeError, ValueError) as exc:
        print(f"First-admin bootstrap refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
