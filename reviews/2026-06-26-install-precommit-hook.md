# Review — install pre-commit hook on hermes-agent repo (2026-06-26)

> **This file is the review artifact for the commit that installs the
> pre-commit hook on `~/.hermes/hermes-agent`.** It exists to satisfy
> the anti-drift gate that the same hook enforces.

## Context

The 2026-06-26 incident exposed a gap: the hermes-agent repo had no
pre-commit hook installed (only `.sample` files). The /card commit
(`de841ce5f4`) landed on 2026-06-25 without review because no hook
existed to enforce it.

Per Opus 4.8 BLOCK on 2026-06-26: do NOT backfill `reviews/<sha>.md`
for `de841ce5f4` (would defeat the gate's audit guarantee). Instead:
install the hook going forward so the NEXT commit cannot land without
review. This is the structural fix.

## The install

- Copy: `scripts/hermes_precommit.py` from `~/hermes_pipelines`
- Symlink: `.git/hooks/pre-commit -> scripts/hermes_precommit.py`
- Same hook, same kill-switch (`HERMES_REVIEW_GATE=0`), same tier logic.

## Tier semantics for hermes-agent

This repo has NO auto-push heartbeat. The `state_pushed/` exemption
that fires in `~/hermes_pipelines` will not trigger here (no commits
will ever be pure `state_pushed/` in hermes-agent). That's fine —
the tier-check is inert when no auto-tier files exist. Manual-tier
review gate is the active enforcement.

## Verification (4 probes, all green)

| Probe | Staged set | Expected | Actual |
|-------|-----------|----------|--------|
| 1 | `state_pushed/health/feed.json` only | PASS (auto-tier) | landed |
| 2 | `state_pushed/` + `manual_script.py` | FAIL (default-deny) | blocked |
| 3 | `manual_script.py` only, no review | FAIL | blocked |
| 4 | `manual_script.py` + `reviews/*.md` | PASS | landed |
| 5 | anything + `HERMES_REVIEW_GATE=0` | PASS (kill switch) | landed |

Probes run in `/tmp/hook_probe2` with the symlink to
`hermes-agent/scripts/hermes_precommit.py`. Additional live probe in
real hermes-agent repo correctly blocked a no-review commit.

## What is NOT done (per Opus BLOCK)

| Item | Status |
|------|--------|
| Backfill `reviews/de841ce5f4.md` | **NEVER** — defeats audit guarantee |
| Reword `de841ce5f4` commit | Not needed; hash stability preserved |
| CHANGELOG entry for de841ce5f4 | Skipped — the audit trail files ARE the documentation |

## Verdict

**APPROVE** — install the hook on hermes-agent. Going forward, every
commit on this repo will be gated. Pre-existing unreviewed commit
(`de841ce5f4`) is documented in the 2026-06-26 GLM/Opus audit trail
at `/tmp/glm_v3_verdict.txt` + `/tmp/opus_verdict_v1.txt`.
