# /card recovery review — 2026-07-03

## Findings

- The Telegram photo handler existed but the plain `/card` command fell through to the generic unknown-command path.
- The handler used `tempfile` without importing it.
- Single-card results supplied an empty `header_summary`, which was passed to Telegram as an invalid empty edit.
- The pipeline embedded its chosen portrait in the vCard but did not expose the image for a separate Telegram attachment.

## Changes reviewed

- Registered a dedicated `/card` command before generic command dispatch; it arms the next photo for five minutes while retaining the caption shortcut.
- Added usage guidance, fixed the missing import and non-empty status fallback, and attached the generated portrait.
- Suppressed duplicate generic image-chat dispatch for card photos, including armed next-photo mode.
- Added inline approval callbacks for unverified online candidates; only an explicit `Use this photo` click sends a replacement photo-embedded vCard.
- Added `/card` to the command registry while keeping it Telegram-specific for Slack's capped native-command list.

## Verification

- Relevant Hermes command suites: 176 passed.
- Adapter and pipeline imports: passed.
- Mock Telegram integration: command guidance, duplicate-chat suppression, LinkedIn summary, VCF/JPEG delivery, and candidate approval callback all passed.
- `git diff --check`: clean.
