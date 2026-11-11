# Security

## Threat model

- Every incoming message is untrusted: text, a voice-note transcript and text inside an image are all possible prompt injections.
- The risk is the "lethal trifecta": private chats, untrusted content, and the ability to send. The design removes the last leg: the model can only propose, `send_message` is the single exit.
- Every send crosses the gate: kill switch, per-chat policy, allowlist, content rules (IBAN, credentials, groups, first contact), rate limit, human approval, audit log. Unknown or malformed policy fails closed.
- Covered by 15 deterministic red-team tests (`pytest -m redteam`); not covered: a compromised host, a malicious approver, an adaptive attacker against a model in the loop, and the real Telegram bridge against a live account (unit-tested with a fake Telethon client only; never exercised against Telegram in CI).
- Secrets (API keys, Telegram `api_id`/`api_hash`) stay in `.env`, which is git-ignored; nothing in this repo needs a key to run the demo or the tests.

## Telegram session file

- `./data/telegram/gma.session` is **full access to the Telegram account**: whoever holds it can read every chat and send as the owner, with no password or 2FA prompt. Keep it private: mode 600 in a 700 directory (the login command sets both), never commit it (`data/`, `*.session` are git-ignored), never put it in an image, a backup you share, or a bug report. If it leaks, revoke it in Telegram under Settings > Devices (terminate the session) and log in again.
- Anyone who can reach the MCP server can read the ingested chats. The compose file binds port 8808 to `127.0.0.1` only; keep it that way and do not expose it through a tunnel or reverse proxy without authentication in front.
- The session is mounted read-write into the worker and the MCP container; a compromise of either gives the attacker the account. The gate protects against a model being talked into sending, not against code execution on the host.
- The bridge never marks messages as read and its only write is `send_text`, which is called only by the gate after policy and (unless an auto policy allows it) human approval. This is asserted with a fake client in tests, not against the live API.
- `python -m shared.telegram_login` is the only code that asks for your phone, login code and 2FA password; it is interactive and never run by tests or CI.

## Reporting a vulnerability

Open an issue at https://github.com/camilbenameur/guarded-messaging-agent/issues. For a bypass of the gate, describe the steps without a working exploit against third parties; the repo has no deployed service, so a public issue is fine.
