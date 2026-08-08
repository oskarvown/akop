"""Unit coverage for REVOKED lifecycle helpers."""
from __future__ import annotations

from app.domain.models import SourceFileLifecycle


def test_source_file_lifecycle_includes_revoked() -> None:
    assert {item.value for item in SourceFileLifecycle} == {
        "active",
        "superseded",
        "revoked",
    }
