"""Deterministic PII redaction for Stage 4.4 LLM payloads (redaction_version=1)."""
from __future__ import annotations

import re

REDACTION_VERSION = "1"

# Phones: +7 / 8 / spaced / dashed Russian-style and generic digit runs with separators.
_PHONE = re.compile(
    r"(?<!\d)(?:\+?7|8)?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Bank / settlement accounts and long digit identifiers (10+ digits, optional spaces).
_ACCOUNT = re.compile(r"(?<!\d)(?:\d[\s\-]?){10,}(?!\d)")
_INN = re.compile(r"(?<!\d)(?:\d{10}|\d{12})(?!\d)")


def redact_comment_text(text: str, *, redaction_version: str = REDACTION_VERSION) -> str:
    """Return redacted comment text for the given redaction policy version."""
    if redaction_version != REDACTION_VERSION:
        raise ValueError(f"unsupported_redaction_version:{redaction_version}")
    out = _EMAIL.sub("[EMAIL]", text)
    out = _PHONE.sub("[PHONE]", out)
    out = _INN.sub("[ID]", out)
    out = _ACCOUNT.sub("[ACCOUNT]", out)
    return out


def redact_counterparty_label(
    label: str | None, *, redaction_version: str = REDACTION_VERSION
) -> str | None:
    if label is None:
        return None
    return redact_comment_text(label, redaction_version=redaction_version)
