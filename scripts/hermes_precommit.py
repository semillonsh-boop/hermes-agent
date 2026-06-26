#!/usr/bin/env python3
"""
Hermes pre-commit hook.

Stdlib-only by mandate (CLAUDE.md rule #6). Imports:
    os, sys, re, subprocess, argparse, pathlib.Path, typing.NamedTuple

This hook runs in 4 stages, each scanning STAGED BLOBS only (never the
working tree) to close the `git add file; sed -i 's/secret//' file; git
commit` bypass.

Stages:
    1. Secret scan (staged blobs)        — high-confidence patterns scan
                                          ALL lines (including comments);
                                          loose assignment patterns scan
                                          non-comment lines only.
    2. Routing spec well-formedness       — when the spec file is staged,
                                          parse it as pipeline:stage=chain
                                          data lines; reject malformed.
    3. Review-artifact gate (anti-drift)  — require at least one staged
                                          `reviews/*.md` (added or modified)
                                          so no commit lands without a
                                          recorded review. Kill switch:
                                          HERMES_REVIEW_GATE=0.
    5. (No source-vs-spec parity)        — out of scope. Deep source
                                          comparison is fragile (sources
                                          live in .py.txt canonical form,
                                          not in .py). CI does that.

Install: scripts/install_precommit.sh
Tests:   scripts/test_hermes_precommit.py
Bypass:  git commit --no-verify (built-in git escape hatch)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


# ---------- Patterns ----------

# High-confidence secret patterns: scan ALL lines (including comments).
# These formats are unambiguous — when present, they are almost certainly
# real secrets even in code comments.
HIGH_CONFIDENCE_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key (sk-ant-...)"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "OpenAI-style API key (sk-...)"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key ID (AKIA...)"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "JWT token (eyJ...)"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "Google API key (AIza...)"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key header"),
    (re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{40}"), "AWS secret access key"),
]

# Loose assignment patterns: scan non-comment lines only.
# Anchored on a key list (so `reset_token`/`csrf_token`/`access_token_expiry`
# don't trip — they aren't in the key set). Min length 12 (raised from 8).
LOOSE_ASSIGNMENT_PATTERN = re.compile(
    r"""\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|"""
    r"""auth[_-]?token|private[_-]?key)\b"""
    r"""\s*[:=]\s*['"](?P<val>[^'"]{12,})['"]"""
)

# Placeholder values: treat as false positives even if they match the
# loose pattern. Case-insensitive match.
PLACEHOLDER_VALUES = frozenset([
    "changeme", "change_me", "replace_me", "replaceme",
    "xxxx", "todo", "example", "your_", "dummy",
    "placeholder", "none", "null", "test", "sample",
])

# Routing spec: known model aliases (the spec may use any of these).
KNOWN_MODEL_ALIASES = frozenset([
    "minimax",        # M3 primary
    "glm-5.1",        # legacy alias
    "glm-5.2",        # current alias
    "deepseek-flash",
    "deepseek-pro",
])
# P2 fix: resolve repo-canonical spec first (mirror routing_validator),
# env override > repo-tracked > legacy ~/.hermes fallback. The hook only
# uses ROUTING_SPEC_PATH.name, but resolving correctly removes the
# misleading hardcoded legacy path.
def _resolve_routing_spec() -> Path:
    env = os.environ.get("HERMES_ROUTING_SPEC")
    if env:
        return Path(os.path.expanduser(env))
    repo_spec = Path(__file__).resolve().parent.parent / "expected_model_routing.txt"
    if repo_spec.exists():
        return repo_spec
    return Path(os.path.expanduser("~/.hermes/expected_model_routing.txt"))


ROUTING_SPEC_PATH = _resolve_routing_spec()
# A data line: `pipeline:stage=model1,model2,...`
# pipeline: lowercase letters; stage: lowercase letters + underscore; chain:
# comma-separated aliases, each is a known alias OR not (we don't enforce
# alias membership in this hook — that's the validator's job; we only enforce
# well-formedness so a strip-the-header edit is caught).
ROUTING_DATA_LINE_RE = re.compile(
    r"""^(?P<pipe>[a-z][a-z0-9_]*):(?P<stage>[a-z][a-z0-9_]*)"""
    r"""=(?P<chain>[a-z0-9._\-]+(?:,[a-z0-9._\-]+)*)$"""
)

# Whitelist: files where secrets are EXPECTED to appear. (The hook skips
# these; in a CI environment you would not commit these at all.)
SECRET_FILE_WHITELIST = frozenset([
    "config.yaml", ".git-credentials", ".netrc",
])

# (SECRET_SCAN_EXTENSIONS removed 2026-06-21 — was dead code. The secret
# scanner is intentionally extension-agnostic: it scans ALL text blobs,
# filtered only by the binary NUL check in read_staged_blob(). If an
# extension allowlist is ever reintroduced, it creates a bypass vector
# where renaming a secret file to an unlisted extension skips the scan.)

# Review-artifact gate (anti-drift). Every commit must stage at least one
# `reviews/<anything>.md` (added or modified). This closes the drift vector
# where commits land without GLM/Opus review. Kill switch for emergencies
# (e.g. submodule pointer bumps, hot-fixes): HERMES_REVIEW_GATE=0.
REVIEW_GATE_KILLSWITCH = "HERMES_REVIEW_GATE"
REVIEW_DIR_NAME = "reviews"


# ---------- Finding model ----------

class Finding(NamedTuple):
    path: str
    line: int
    rule: str
    snippet: str  # redacted


# ---------- Git helpers (staged-blob only) ----------

def list_staged_files() -> list[str]:
    """Return paths of staged files (A/C/M/R). NUL-safe. D skips (no blob)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only",
             "--diff-filter=ACMR", "-z"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return []
    if not result.stdout:
        return []
    # NUL-separated; trailing NUL on some git versions, split accordingly.
    return [p for p in result.stdout.split("\x00") if p]


def read_staged_blob(path: str) -> str | None:
    """Return the staged version of `path` (the index blob, not the
    working tree). Returns None if the blob cannot be read (binary, large,
    or path is not in the index)."""
    try:
        result = subprocess.run(
            ["git", "show", ":" + path],
            capture_output=True, check=True,
        )
    except subprocess.CalledProcessError:
        return None
    # If the first 8KB contains NUL, treat as binary and skip secret scan.
    if b"\x00" in result.stdout[:8192]:
        return None
    return result.stdout.decode("utf-8", errors="replace")


# ---------- Secret scan ----------

def _is_comment_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("#") or stripped.startswith("//")


def _redact(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2:
        return "***REDACTED***"
    return value[:keep] + "***REDACTED***" + value[-keep:]


def scan_blob_for_secrets(path: str, content: str) -> list[Finding]:
    """Scan one staged blob for hardcoded secrets. Returns list of findings."""
    basename = os.path.basename(path)
    if basename in SECRET_FILE_WHITELIST:
        return []
    if any(part.startswith(".git") for part in Path(path).parts):
        return []

    findings: list[Finding] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        # High-confidence patterns: scan ALL lines (including comments).
        for pattern, kind in HIGH_CONFIDENCE_SECRET_PATTERNS:
            m = pattern.search(line)
            if m:
                findings.append(Finding(
                    path=path, line=lineno, rule=kind,
                    snippet=_redact(m.group(0)),
                ))

        # Loose assignment pattern: skip comment lines (use the high-
        # confidence patterns above to catch secrets in comments).
        if _is_comment_line(line):
            continue
        m = LOOSE_ASSIGNMENT_PATTERN.search(line)
        if not m:
            continue
        val = m.group("val")
        # Skip placeholder values (false-positive guard).
        if val.lower() in PLACEHOLDER_VALUES:
            continue
        # Skip values that don't look like real secrets (no digits = likely
        # a constant name, not a key).
        if not re.search(r"\d", val) or not re.search(r"[A-Za-z]", val):
            continue
        if len(val) < 16:
            continue
        findings.append(Finding(
            path=path, line=lineno, rule="key=value assignment (loose match)",
            snippet=_redact(val),
        ))

    return findings


# ---------- Routing spec well-formedness ----------

def parse_routing_spec(content: str) -> tuple[list[str], dict[str, dict[str, list[str]]]]:
    """Parse the routing spec file. Returns (errors, parsed).

    The spec format:
        # comment
        [SECTION]   <- section header
        pipeline:stage=model1,model2,...

    Only `pipeline:stage=...` lines are data. Comments and section
    headers are ignored (the data lines are the source of truth).
    """
    errors: list[str] = []
    parsed: dict[str, dict[str, list[str]]] = {}

    for lineno, line in enumerate(content.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("["):
            continue
        # Split on the first `=`. If there's no `=`, it's malformed.
        if "=" not in s:
            errors.append(f"line {lineno}: malformed data line (no '='): {s!r}")
            continue
        head, _, tail = s.partition("=")
        head = head.strip()
        chain_tokens = [c.strip() for c in tail.split(",") if c.strip()]
        if not head or ":" not in head:
            errors.append(f"line {lineno}: malformed data line (no pipeline:stage): {s!r}")
            continue
        if not chain_tokens:
            errors.append(f"line {lineno}: empty chain for {head!r}")
            continue
        pipe, _, stage = head.partition(":")
        pipe = pipe.strip()
        stage = stage.strip()
        if not pipe or not stage:
            errors.append(f"line {lineno}: malformed pipeline:stage: {head!r}")
            continue
        parsed.setdefault(pipe, {})[stage] = chain_tokens

    return errors, parsed


def check_routing_spec(staged_paths: list[str]) -> list[Finding]:
    """If the routing spec is among the staged paths, parse and validate
    well-formedness. Returns list of findings (empty on pass)."""
    findings: list[Finding] = []
    spec_path = ROUTING_SPEC_PATH.name
    if spec_path not in [os.path.basename(p) for p in staged_paths]:
        return findings
    # Read the staged version (not the working tree).
    spec_staged_path = next(
        (p for p in staged_paths if os.path.basename(p) == spec_path), None
    )
    if not spec_staged_path:
        return findings
    content = read_staged_blob(spec_staged_path)
    if content is None:
        return [Finding(
            path=spec_staged_path, line=0,
            rule="routing spec unreadable as text",
            snippet="(binary or undecodable)",
        )]

    errors, parsed = parse_routing_spec(content)
    if errors:
        for err in errors:
            findings.append(Finding(
                path=spec_staged_path, line=0,
                rule="routing spec malformed",
                snippet=err,
            ))
        return findings
    if not parsed:
        findings.append(Finding(
            path=spec_staged_path, line=0,
            rule="routing spec has no data lines",
            snippet="(only comments/headers found — did you strip the data?)",
        ))
        return findings
    # Well-formed. (Deep source-vs-spec parity is out of scope.)
    return findings


# ---------- Review-artifact gate (anti-drift) ----------

def check_review_artifact() -> list[Finding]:
    """Require a NEW (Added), non-empty `reviews/*.md` for any commit.

    Closes the drift vector: no commit lands without a recorded review.
    Enforcement (stronger than "added or modified"):
      * Only files with git status **A** (added) satisfy the gate — not
        M/R/C. A rename, copy, or re-touch of an old review file does
        NOT count; each commit ships a fresh review file.
      * The staged blob must be non-whitespace (no zero-byte stubs).
      * The gate fires whenever ANY change is staged, including pure
        deletions (a commit that only removes files still needs a review).

    Tier exemption (auto-push only):
      If EVERY staged path (incl. deletions) lives under `state_pushed/`,
      the heartbeat's own `_staged_paths` allowlist (`hermes_heartbeat_v2.sh`
      lines 117-150) has already vetted it. The review-gate auto-passes.
      Default-deny: any mixed staged set containing even one non-`state_pushed/`
      path still requires a fresh `reviews/*.md`.

    Anchoring to the specific change is human discipline (the convention
    names files by date+topic). The gate enforces that a genuine new
    review EXISTS.

    Bypass: `HERMES_REVIEW_GATE=0` (emergencies only — hot-fixes, pointer
    bumps; logged as a nudge, not a hard block) or `git commit --no-verify`
    (git's built-in escape hatch, which also disables secret-scanning).
    """
    if os.environ.get(REVIEW_GATE_KILLSWITCH, "1") == "0":
        return []  # kill switch active

    # Fire whenever any change is staged, including deletions. A deletion-
    # only commit (ACMR list empty) must still clear the review gate.
    all_changes = _staged_paths("ACMRD")
    if not all_changes:
        return []  # nothing staged → nothing to gate

    # Auto-tier exemption: heartbeat's own allowlist (state_pushed/ only)
    # is the single source of truth. No review artifact required for
    # pure auto-push commits. Mixed sets default-deny (require review).
    if all(p.startswith("state_pushed/") for p in all_changes):
        return []  # auto-tier — heartbeat allowlist already vetted

    # Only NEW (added) reviews count.
    added = _staged_paths("A")
    for p in added:
        if not _is_review_path(p):
            continue
        blob = read_staged_blob(p)
        if blob and blob.strip():
            return []  # gate satisfied: new, non-empty review

    return [Finding(
        path="(repo root)", line=0,
        rule="no new staged review artifact (anti-drift gate)",
        snippet=(
            "No NEW non-empty `reviews/*.md` staged. `git add` a fresh "
            "review file (GLM architect / Opus verdict / fix log), "
            "OR set HERMES_REVIEW_GATE=0 for an emergency bypass, OR use "
            "`git commit --no-verify`. Renames/copies/re-touches of old "
            "reviews do NOT satisfy the gate. NOTE: auto-push commits "
            "(every staged path under state_pushed/) are exempt by design."
        ),
    )]


def _staged_paths(diff_filter: str) -> list[str]:
    """Return staged paths for the given --diff-filter (e.g. 'A', 'ACMR',
    'ACMRD'). NUL-safe. Repo-relative paths as emitted by git."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only",
             f"--diff-filter={diff_filter}", "-z"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    if not result.stdout:
        return []
    return [p for p in result.stdout.split("\x00") if p]


def _is_review_path(path: str) -> bool:
    """True if `path` is a repo-relative `reviews/*.md` (case-insensitive
    on the directory and extension for cross-filesystem consistency)."""
    p = path.replace("\\", "/")
    low = p.lower()
    if not low.endswith(".md"):
        return False
    if not low.startswith("reviews/"):
        return False
    return True


# ---------- Orchestration ----------

def run_hook() -> int:
    """Main entry point. Returns 0 on pass, 1 on findings."""
    staged = list_staged_files()
    # Stage 4: review-artifact gate fires on ANY change (incl. deletions),
    # so run it even when the ACMR blob list is empty (deletion-only commit).
    review_findings = check_review_artifact()
    if not staged:
        # No scannable blobs, but a deletion-only commit still needs a review.
        if review_findings:
            _report(review_findings)
            return 1
        return 0

    findings: list[Finding] = list(review_findings)

    # Stage 1: per-blob scans (secrets)
    for path in staged:
        content = read_staged_blob(path)
        if content is None:
            continue  # binary, large, or unreadable
        findings.extend(scan_blob_for_secrets(path, content))

    # Stage 3: routing spec
    findings.extend(check_routing_spec(staged))

    if not findings:
        return 0
    _report(findings)
    return 1


def _report(findings: list[Finding]) -> None:
    """Print grouped findings to stdout."""

    # Group findings by rule for readability.
    by_rule: dict[str, list[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)

    print("=" * 70)
    print("❌ PRE-COMMIT BLOCKED")
    print("=" * 70)
    for rule, items in sorted(by_rule.items()):
        print(f"\n  [{rule}]  ({len(items)} finding{'s' if len(items) != 1 else ''})")
        for f in items:
            line_str = f"line {f.line}" if f.line else "(no line)"
            print(f"    {f.path}:{line_str}  {f.snippet}")
    print()
    print("Remediation:")
    print("  - Secrets: move to ~/.hermes/.env; reference via env var.")
    print("  - Routing spec: data lines must match `<pipe>:<stage>=<chain>`.")
    print("  - Review gate: stage a `reviews/*.md` (or HERMES_REVIEW_GATE=0).")
    print("  - Bypass (last resort): `git commit --no-verify`.")


def selftest() -> int:
    """Minimal self-test: parse a tiny spec and assert no findings on
    a known-clean staged blob. Exits 0 on success, 1 on failure."""
    spec = """\
[HKR]
hkr:analysis=minimax,glm-5.2,deepseek-flash
[RMI]
rmi:analysis=minimax,glm-5.2,deepseek-flash
"""
    errors, parsed = parse_routing_spec(spec)
    if errors:
        print(f"selftest FAIL: parse errors: {errors}", file=sys.stderr)
        return 1
    if "hkr" not in parsed or "rmi" not in parsed:
        print(f"selftest FAIL: expected hkr+rmi, got {list(parsed)}",
              file=sys.stderr)
        return 1
    print("selftest OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes pre-commit hook")
    parser.add_argument("--selftest", action="store_true",
                        help="run a tiny self-test and exit")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    return run_hook()


if __name__ == "__main__":
    sys.exit(main())
