"""Build the configured `Bridge` from environment variables.

- `BRIDGE=fake` (default): `FakeBridge` replaying `BRIDGE_SCENARIO`, or None
  when no scenario is set (the send gate then fails closed).
- `BRIDGE=telegram`: `TelethonBridge` from `TELEGRAM_API_ID`,
  `TELEGRAM_API_HASH` and `TELEGRAM_SESSION_PATH`. Building it does not touch
  the network.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from shared.bridge import Bridge

DEFAULT_SESSION_PATH = "./data/telegram/gma.session"


class BridgeConfigError(ValueError):
    """The environment does not describe a usable bridge."""


def telegram_credentials(env: Mapping[str, str]) -> tuple[int, str, str]:
    api_id_raw = (env.get("TELEGRAM_API_ID") or "").strip()
    api_hash = (env.get("TELEGRAM_API_HASH") or "").strip()
    if not api_id_raw or not api_hash:
        raise BridgeConfigError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are required (get them at "
            "https://my.telegram.org -> API development tools)."
        )
    try:
        api_id = int(api_id_raw)
    except ValueError:
        raise BridgeConfigError("TELEGRAM_API_ID must be an integer.") from None
    return api_id, api_hash, (env.get("TELEGRAM_SESSION_PATH") or DEFAULT_SESSION_PATH).strip()


def bridge_kind(env: Mapping[str, str]) -> str:
    kind = (env.get("BRIDGE") or "fake").strip().lower()
    if kind not in ("fake", "telegram"):
        raise BridgeConfigError(f"BRIDGE must be 'fake' or 'telegram', got {kind!r}.")
    return kind


def build_bridge(
    env: Mapping[str, str] | None = None, *, keep_connected: bool = False
) -> Bridge | None:
    env = os.environ if env is None else env
    if bridge_kind(env) == "telegram":
        from shared.telethon_bridge import TelethonBridge

        api_id, api_hash, session = telegram_credentials(env)
        return TelethonBridge(
            api_id,
            api_hash,
            session,
            keep_connected=keep_connected,
            include_channels=(env.get("TELEGRAM_INCLUDE_CHANNELS") or "").lower()
            in ("1", "true", "yes", "on"),
            backfill_days=float(env.get("TELEGRAM_BACKFILL_DAYS") or 7),
        )
    scenario = env.get("BRIDGE_SCENARIO")
    if scenario:
        from shared.fake_bridge import FakeBridge

        return FakeBridge(scenario)
    return None
