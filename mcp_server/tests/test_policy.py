"""Unit tests for `mcp_server.policy.evaluate` — the pure decision function
behind the send gate. Adversarial end-to-end coverage lives in
`test_redteam.py`; this file pins the evaluation semantics themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp_server.policy import Decision, SendContext, contains_money_or_credentials, evaluate

NOON = int(datetime(2025, 7, 7, 12, 0, tzinfo=UTC).timestamp())
THREE_AM = int(datetime(2025, 7, 7, 3, 0, tzinfo=UTC).timestamp())


def ctx(**overrides: Any) -> SendContext:
    defaults: dict[str, Any] = {"chat_id": "chat-1", "text": "hello there", "now": NOON}
    defaults.update(overrides)
    return SendContext(**defaults)


def full_auto() -> dict[str, Any]:
    return {
        "mode": "auto_validé",
        "auto_reply": {"enabled": True, "allow_when": {}, "never_when": {}},
        "rate_limit": {"max_auto_per_hour": 3},
    }


# -- default / mode handling -----------------------------------------------------


def test_no_policy_queues() -> None:
    decision = evaluate(None, ctx())
    assert decision.outcome == "queue"
    assert "no policy" in decision.reason


def test_manual_and_assisted_modes_queue_in_any_spelling() -> None:
    for mode in ("manuel", "manual", "assisté", "assiste", "assisted"):
        decision = evaluate({"mode": mode}, ctx())
        assert decision.outcome == "queue", mode


def test_unknown_mode_denies_fail_closed() -> None:
    decision = evaluate({"mode": "wide-open"}, ctx())
    assert decision == Decision("deny", "unrecognized policy mode 'wide-open' — failing closed")


def test_auto_mode_with_auto_reply_disabled_queues() -> None:
    policy = full_auto()
    policy["auto_reply"]["enabled"] = False
    assert evaluate(policy, ctx()).outcome == "queue"


def test_auto_mode_all_conditions_met_allows() -> None:
    decision = evaluate(full_auto(), ctx())
    assert decision.outcome == "allow"
    assert "auto-policy" in decision.reason


# -- allow_when conditions fail closed on missing context -------------------------


def test_intent_required_but_unknown_queues() -> None:
    policy = full_auto()
    policy["auto_reply"]["allow_when"]["intent_in"] = ["logistique"]
    assert evaluate(policy, ctx(intent=None)).outcome == "queue"
    assert evaluate(policy, ctx(intent="logistique")).outcome == "allow"


def test_confidence_below_min_or_unknown_queues() -> None:
    policy = full_auto()
    policy["auto_reply"]["allow_when"]["confidence_min"] = 0.85
    assert evaluate(policy, ctx(confidence=None)).outcome == "queue"
    assert evaluate(policy, ctx(confidence=0.5)).outcome == "queue"
    assert evaluate(policy, ctx(confidence=0.9)).outcome == "allow"


def test_business_hours_only_queues_outside_window() -> None:
    policy = full_auto()
    policy["auto_reply"]["allow_when"]["business_hours_only"] = True
    assert evaluate(policy, ctx(now=THREE_AM)).outcome == "queue"
    assert evaluate(policy, ctx(now=NOON)).outcome == "allow"


def test_rate_limit_boundary() -> None:
    policy = full_auto()  # max_auto_per_hour = 3
    assert evaluate(policy, ctx(auto_sends_last_hour=2)).outcome == "allow"
    assert evaluate(policy, ctx(auto_sends_last_hour=3)).outcome == "queue"


# -- money / credentials heuristic -------------------------------------------------


def test_money_or_credentials_detector() -> None:
    assert contains_money_or_credentials("IBAN: FR76 3000 6000 0112 3456 7890 189")
    assert contains_money_or_credentials("card 4111 1111 1111 1111 exp 12/26")
    assert contains_money_or_credentials("je te dois 250€ pour le week-end")
    assert contains_money_or_credentials("le mot de passe est azerty123")
    assert contains_money_or_credentials("here's my api key for the staging box")
    assert contains_money_or_credentials("ton code de vérification est là") is not None
    # Benign content — including a phone number — passes.
    assert contains_money_or_credentials("on se voit demain à 15h ?") is None
    assert contains_money_or_credentials("call me at +33 6 00 00 00 00") is None
