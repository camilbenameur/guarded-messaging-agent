# Security

## Threat model

- Every incoming message is untrusted: text, a voice-note transcript and text inside an image are all possible prompt injections.
- The risk is the "lethal trifecta": private chats, untrusted content, and the ability to send. The design removes the last leg: the model can only propose, `send_message` is the single exit.
- Every send crosses the gate: kill switch, per-chat policy, allowlist, content rules (IBAN, credentials, groups, first contact), rate limit, human approval, audit log. Unknown or malformed policy fails closed.
- Covered by 15 deterministic red-team tests (`pytest -m redteam`); not covered: a compromised host, a malicious approver, an adaptive attacker against a model in the loop, and the real Telegram bridge (not wired yet).
- Secrets (API keys, Telegram session) stay in `.env`, which is git-ignored; nothing in this repo needs a key to run the demo or the tests.

## Reporting a vulnerability

Open an issue at https://github.com/camilbenameur/guarded-messaging-agent/issues. For a bypass of the gate, describe the steps without a working exploit against third parties; the repo has no deployed service, so a public issue is fine.
