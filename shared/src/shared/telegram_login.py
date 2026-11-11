"""One-time interactive Telegram login, run by the account OWNER:

    python -m shared.telegram_login

Reads TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION_PATH from the
environment (or `.env` in the current directory), asks for your phone number,
the login code Telegram sends you and, if enabled, your 2FA password, in the
terminal, and writes the session file (mode 600). That file is full access to
the account: keep it private. Nothing here runs in CI or from the other modules.

    python -m shared.telegram_login --status   # offline: does the session file exist?
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared.bridge_factory import BridgeConfigError, telegram_credentials


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def session_file(session_path: str) -> Path:
    p = Path(session_path)
    return p if p.suffix == ".session" else p.with_name(p.name + ".session")


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[..., Any] | None = None,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.telegram_login", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--status", action="store_true", help="offline check that the session file exists"
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    try:
        api_id, api_hash, session_path = telegram_credentials(os.environ)
    except BridgeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    target = session_file(session_path)

    if args.status:
        print(f"session file {target}: {'present' if target.exists() else 'missing'}")
        return 0 if target.exists() else 1

    if client_factory is None and not sys.stdin.isatty():
        print("error: run this in an interactive terminal.", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)

    if client_factory is None:
        from telethon import TelegramClient

        client_factory = TelegramClient
    client = client_factory(str(target), api_id, api_hash)

    async def login() -> str:
        await client.start(
            phone=lambda: input_fn(
                "Phone number (international format, e.g. +33612345678): "
            ).strip(),
            code_callback=lambda: input_fn("Login code sent by Telegram: ").strip(),
            password=lambda: password_fn("2FA password (input hidden): "),
        )
        me = await client.get_me()
        await client.disconnect()
        return getattr(me, "username", None) or str(getattr(me, "id", "?"))

    who = asyncio.run(login())
    if target.exists():
        os.chmod(target, 0o600)
    print(f"logged in as {who}; session saved to {target}")
    print("This file is full access to your Telegram account. Never commit or share it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
