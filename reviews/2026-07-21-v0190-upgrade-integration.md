# Hermes v0.19.0 integration review

Date: 2026-07-21

## Decision

Upgrade the live gateway from v0.18.0 to the signed `v2026.7.20` tag on a dedicated integration branch. Do not track floating `main` for this cutover.

## Preserved custom behaviour

- `/card` business-card ingestion and approval flow.
- The anti-drift pre-commit review gate.
- MarkdownV2 entity-aware fallback chunking, because upstream still uses generic `truncate_message()` in the legacy Telegram send path.

## Integration corrections

- Resolve the command registry against the v0.19 command names (`topup`, not retired `credits` / `billing`).
- Preserve all new upstream Telegram imports alongside the `/card` imports.
- Route `/card` through Hermes' standard authorised-user check rather than the optional `S_TELEGRAM_CHAT_ID` environment variable.

## Rejected paths

- Blind `hermes update` to floating `origin/main`: rejected because the live checkout carried four local commits and upstream rewrote the Telegram adapter heavily.
- Dropping the `/card` patch: rejected because it is a live user workflow with no upstream equivalent.
- Dropping MarkdownV2 fallback chunking without testing: rejected because upstream still has the generic split-plus-plain-text fallback path that caused the original visible corruption.

## Required cutover checks

1. Static compilation and targeted Telegram/command tests.
2. Config migration from schema 24 to 32 without changing the model chain.
3. Gateway restart under the existing system service and environment override.
4. Real Telegram reply beginning with `s· `.
5. Long formatted Telegram message and `/card` flow verification.
6. Direct API health/model verification and rollback branch confirmation.

## Live outcome

- Focused upstream plus custom regression suite: 393 passed.
- Hermes Doctor: no active security advisory, runtime dependency failure, deprecated key, or provider-connectivity failure. Two build-time npm advisories remain in the web/UI workspaces and do not affect the gateway runtime.
- Configuration migrated from schema 24 to 33. The live model chain remained `deepseek-v4-flash` -> `glm-5.2` -> `deepseek-v4-pro`.
- The first external 75-line Telegram test exposed a v0.19 streaming-finalisation defect: item 63 was lost and item 64 was split. Raw `state.db` content was complete, proving the defect was transport-side rather than model-side.
- Mitigation: `display.platforms.telegram.streaming: false`. Telegram rich messages and rich drafts were already disabled. The repeated external test delivered all items 1-75 across two complete chunks.
- Live Telegram reply after the final restart began with the real `s· ` canary and reported v0.19.0 healthy.
- `/card` registry, chunk integrity, and authorised-user behaviour are covered by `tests/gateway/test_shelson_telegram_customizations.py`. A real photo was not uploaded because that would create contact artefacts from a non-source test image.
