"""Per-chat automation policy model + evaluation.

A policy is a JSON object
stored on the chat row (`shared.store.Store.get_policy`/`set_policy`):

    {
      "mode": "assisté",                      # manual | assisted | auto-validated
                                              # (stored values: manuel | assisté | auto_validé)
      "auto_reply": {
        "enabled": false,
        "allow_when": {
          "sender_in": [123456789, "@alice"],
          "intent_in": ["logistique", "confirmation_rdv"],
          "confidence_min": 0.85,
          "business_hours_only": true
        },
        "never_when": {
          "contains_money_or_credentials": true,
          "is_group": true,
          "first_message_from_contact": true
        }
      },
      "rate_limit": { "max_auto_per_hour": 3 }
    }

`evaluate(policy, context)` maps a send request onto one of three outcomes:

- ``allow`` — auto-send permitted: mode is auto (auto-validated), `auto_reply`
  is enabled, no `never_when` rule fires, every `allow_when` condition holds,
  and the hourly auto-send budget isn't spent.
- ``queue`` — park in the human-approval queue. This is the default posture:
  no policy row, manual/assisted mode, or an auto-mode request that misses
  any condition (auto-send only if every policy condition holds, otherwise
  the approval queue).
- ``deny`` — hard refusal, reserved for fail-closed situations (an
  unrecognized/malformed policy). The global kill switch also denies, but at
  the gate level (`mcp_server.gate`), before evaluation.

Everything here is pure and deterministic: the clock, rate-limit counters,
and chat facts arrive via `SendContext`, injected by the gate (and by tests
— no sleeping, no wall-clock reads unless the caller omits `now`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Literal

Outcome = Literal["allow", "queue", "deny"]

MODE_MANUAL = "manual"
MODE_ASSISTED = "assisted"
MODE_AUTO = "auto"

# Policies may name the modes in French (manuel / assisté / auto_validé,
# i.e. manual / assisted / auto-validated); accept those spellings plus
# unaccented and English aliases. Anything else
# fails closed to `deny` in `evaluate`.
_MODE_ALIASES: dict[str, str] = {
    "manual": MODE_MANUAL,
    "manuel": MODE_MANUAL,
    "assisted": MODE_ASSISTED,
    "assisté": MODE_ASSISTED,
    "assiste": MODE_ASSISTED,
    "auto": MODE_AUTO,
    "auto_validé": MODE_AUTO,
    "auto_valide": MODE_AUTO,
    "autovalidé": MODE_AUTO,
    "autovalide": MODE_AUTO,
}

#: `allow_when.business_hours_only` window, hours in UTC (containers run UTC).
#: MVP heuristic — start inclusive, end exclusive.
BUSINESS_HOURS_UTC: tuple[int, int] = (9, 18)


@dataclass(frozen=True)
class Decision:
    """`evaluate`'s verdict. `reason` is always populated and human/audit
    readable — it flows into the audit log and the approval queue as-is.
    """

    outcome: Outcome
    reason: str


@dataclass(frozen=True)
class SendContext:
    """Everything `evaluate` may need to judge one send request. Built by the
    gate from the store + its injected clock; built by hand in tests.
    """

    chat_id: str
    text: str
    is_group: bool = False
    is_first_contact: bool = False
    intent: str | None = None  # no classifier in the MVP -> stays None, fails closed
    confidence: float | None = None
    now: float | None = None  # epoch seconds; None = wall clock (business hours only)
    auto_sends_last_hour: int = 0


# -- money / credentials heuristic (never_when.contains_money_or_credentials) --

# IBAN-shaped: two letters + two check digits + spaced/bare alphanumeric groups.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{2,4}){3,8}\b")
# Card-shaped: 13-19 digits allowing space/dash separators. Short enough
# sequences (phone numbers ~10-12 digits) don't trip it.
_CARD_RE = re.compile(r"(?<![\dA-Za-z])(?:\d[ -]?){12,18}\d(?![\dA-Za-z])")
# Currency amounts: symbol-adjacent digits or an explicit currency code.
_AMOUNT_RE = re.compile(
    r"(?:[€$£]\s?\d|\d\s?[€$£]|\b\d+(?:[.,]\d+)?\s?(?:EUR|USD|GBP|CHF)\b)",
    re.IGNORECASE,
)
_CREDENTIAL_KEYWORDS: tuple[str, ...] = (
    "iban",
    "password",
    "passwd",
    "mot de passe",  # French: password
    "credit card",
    "carte bancaire",  # French: bank card
    "carte bleue",
    "cvv",
    "cvc",
    "code pin",
    "pin code",
    "credential",
    "identifiants",  # French: credentials
    "virement",  # French: wire transfer
    "wire transfer",
    "bank transfer",
    "rib",
    "api key",
    "api_key",
    "secret key",
    "seed phrase",
    "verification code",
    "code de vérification",  # French: verification code
    "2fa",
)


def contains_money_or_credentials(text: str) -> str | None:
    """Best-effort detector for financial/credential content (`never_when` blocks
    money/credentials from ever going out automatically).
    Returns a short human-readable label of what matched, or None. Biased
    toward over-matching — a false positive costs one human approval, a
    false negative leaks money/credentials on auto-pilot.
    """
    if _IBAN_RE.search(text):
        return "IBAN-shaped account number"
    if _CARD_RE.search(text):
        return "card-shaped digit sequence"
    if _AMOUNT_RE.search(text):
        return "currency amount"
    lowered = text.lower()
    for keyword in _CREDENTIAL_KEYWORDS:
        if keyword in lowered:
            return f"credential/finance keyword {keyword!r}"
    return None


# -- evaluation -----------------------------------------------------------------


def evaluate(policy: dict[str, Any] | None, context: SendContext) -> Decision:
    """Map one send request onto allow | queue | deny (see module docstring).

    Fail-closed by construction: the *only* path to ``allow`` is an
    explicitly-enabled auto policy whose every condition checks out; missing
    context (no intent classifier, unknown confidence) counts as a failed
    condition, and an unintelligible policy is a hard ``deny``.
    """
    if policy is None:
        return Decision(
            "queue",
            "no policy configured — default is read-only + propose; queued for human approval",
        )

    mode = _MODE_ALIASES.get(str(policy.get("mode", "")).strip().lower())
    if mode is None:
        return Decision(
            "deny",
            f"unrecognized policy mode {policy.get('mode')!r} — failing closed",
        )
    if mode == MODE_MANUAL:
        return Decision(
            "queue", "policy mode is manual (read-only + propose) — queued for human approval"
        )
    if mode == MODE_ASSISTED:
        return Decision(
            "queue", "policy mode is assisted — every send requires human approval"
        )

    # mode == MODE_AUTO (auto-validated): auto only if *all* conditions hold.
    auto_reply = policy.get("auto_reply") or {}
    if not auto_reply.get("enabled", False):
        return Decision("queue", "auto_reply.enabled is false — queued for human approval")

    never_when = auto_reply.get("never_when") or {}
    if never_when.get("contains_money_or_credentials"):
        hit = contains_money_or_credentials(context.text)
        if hit is not None:
            return Decision(
                "queue",
                f"never_when: content matches money/credentials heuristic ({hit}) — "
                "auto blocked, human approval required",
            )
    if never_when.get("is_group") and context.is_group:
        return Decision("queue", "never_when: group chat — auto-reply blocked")
    if never_when.get("first_message_from_contact") and context.is_first_contact:
        return Decision(
            "queue",
            "never_when: first contact (no prior history with this chat) — auto blocked",
        )

    allow_when = auto_reply.get("allow_when") or {}
    sender_in = allow_when.get("sender_in")
    if sender_in is not None:
        allowlist = {str(entry) for entry in sender_in}
        if str(context.chat_id) not in allowlist:
            return Decision(
                "queue",
                f"allow_when: counterparty {context.chat_id!r} not in allowlist — "
                "queued for human approval",
            )
    intent_in = allow_when.get("intent_in")
    if intent_in is not None and (context.intent is None or context.intent not in intent_in):
        return Decision(
            "queue",
            f"allow_when: intent {context.intent!r} not in permitted intents — queued",
        )
    confidence_min = allow_when.get("confidence_min")
    if confidence_min is not None and (
        context.confidence is None or context.confidence < float(confidence_min)
    ):
        return Decision(
            "queue",
            f"allow_when: confidence {context.confidence!r} below minimum "
            f"{confidence_min} — queued",
        )
    if allow_when.get("business_hours_only"):
        now = time.time() if context.now is None else context.now
        hour = time.gmtime(now).tm_hour
        if not (BUSINESS_HOURS_UTC[0] <= hour < BUSINESS_HOURS_UTC[1]):
            return Decision(
                "queue",
                f"allow_when: outside business hours ({hour:02d}h UTC) — queued",
            )

    rate_limit = policy.get("rate_limit") or {}
    max_auto = rate_limit.get("max_auto_per_hour")
    if max_auto is not None and context.auto_sends_last_hour >= int(max_auto):
        return Decision(
            "queue",
            f"rate limit: {context.auto_sends_last_hour} auto send(s) in the last hour "
            f"reached max_auto_per_hour={max_auto} — queued",
        )

    return Decision(
        "allow",
        "auto-policy: mode auto_validé, all never_when/allow_when/rate-limit "
        "conditions satisfied",
    )
