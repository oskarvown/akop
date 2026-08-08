"""Unit tests for redaction and OpenRouter schema/retry behavior."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.domain.calculations.comment_redaction import (
    redact_comment_text,
    redact_counterparty_label,
)
from app.infrastructure.llm.openrouter_client import (
    OpenRouterClient,
    OpenRouterSchemaError,
    OpenRouterTransientError,
    _parse_facts,
)


def test_redaction_masks_phone_email_account_and_inn() -> None:
    raw = (
        "Звонить +7 (916) 123-45-67 или 89161234567, "
        "почта manager@example.com, "
        "счёт 40817810099910004312, ИНН 7707083893"
    )
    redacted = redact_comment_text(raw)
    assert "+7" not in redacted
    assert "916" not in redacted or "[PHONE]" in redacted
    assert "manager@example.com" not in redacted
    assert "[EMAIL]" in redacted
    assert "40817810099910004312" not in redacted
    assert "[ACCOUNT]" in redacted or "[ID]" in redacted
    assert "7707083893" not in redacted
    label = redact_counterparty_label("ООО Ромашка +79991112233")
    assert label is not None
    assert "+79991112233" not in label


def test_parse_facts_invalid_date_is_schema_error() -> None:
    with pytest.raises(OpenRouterSchemaError, match="invalid date"):
        _parse_facts(
            {
                "mentioned_date": "32-13-99",
                "mentioned_amount": None,
                "action": None,
                "reason": None,
                "responsible_person": None,
                "summary": None,
                "confidence": "low",
            }
        )


def test_parse_facts_invalid_amount_and_confidence() -> None:
    base = {
        "mentioned_date": None,
        "mentioned_amount": "not-a-number",
        "action": None,
        "reason": None,
        "responsible_person": None,
        "summary": None,
        "confidence": "low",
    }
    with pytest.raises(OpenRouterSchemaError, match="invalid amount"):
        _parse_facts(base)
    base["mentioned_amount"] = None
    base["confidence"] = "maybe"
    with pytest.raises(OpenRouterSchemaError, match="invalid confidence"):
        _parse_facts(base)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "nan"])
def test_parse_facts_rejects_non_finite_amount(amount: str) -> None:
    with pytest.raises(OpenRouterSchemaError, match="invalid amount"):
        _parse_facts(
            {
                "mentioned_date": None,
                "mentioned_amount": amount,
                "action": None,
                "reason": None,
                "responsible_person": None,
                "summary": None,
                "confidence": "low",
            }
        )


@pytest.mark.asyncio
async def test_openrouter_retries_transient_http_then_succeeds() -> None:
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
            self.status = status
            self._payload = payload or {}

        async def read(self) -> bytes:
            return b""

        async def text(self) -> str:
            return "err"

        async def json(self, content_type: object = None) -> dict[str, Any]:
            return self._payload

        async def __aenter__(self) -> FakeResp:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        def post(self, url: str, **kwargs: object) -> FakeResp:
            calls["n"] += 1
            assert "sk-secret" not in json.dumps(kwargs.get("json"))
            body = kwargs.get("json")
            assert isinstance(body, dict)
            user = body["messages"][1]["content"]
            assert "89161234567" not in user
            assert "PK\x03\x04" not in user  # xlsx magic
            if calls["n"] < 3:
                return FakeResp(503)
            return FakeResp(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "mentioned_date": "2026-08-15",
                                        "mentioned_amount": "100.00",
                                        "action": "оплата",
                                        "reason": None,
                                        "responsible_person": None,
                                        "summary": "ok",
                                        "confidence": "high",
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        async def close(self) -> None:
            return None

    client = OpenRouterClient(
        api_key="sk-secret",
        base_url="https://example.test/v1",
        model="model-v1",
        timeout_seconds=5,
        max_retries=2,
        session=FakeSession(),  # type: ignore[arg-type]
    )
    facts = await client.analyze_comment(
        comment_raw="уточнить [PHONE]",
        report_date=date(2026, 8, 8),
        counterparty_label="X",
    )
    assert calls["n"] == 3
    assert facts.mentioned_amount == Decimal("100.00")
    assert facts.mentioned_date == date(2026, 8, 15)


@pytest.mark.asyncio
async def test_openrouter_schema_invalid_choices_no_retry() -> None:
    calls = {"n": 0}

    class FakeResp:
        status = 200

        async def json(self, content_type: object = None) -> dict[str, Any]:
            return {"choices": []}

        async def __aenter__(self) -> FakeResp:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        def post(self, *args: object, **kwargs: object) -> FakeResp:
            calls["n"] += 1
            return FakeResp()

        async def close(self) -> None:
            return None

    client = OpenRouterClient(
        api_key="k",
        base_url="https://example.test/v1",
        model="m",
        timeout_seconds=5,
        max_retries=3,
        session=FakeSession(),  # type: ignore[arg-type]
    )
    with pytest.raises(OpenRouterSchemaError, match="missing choices"):
        await client.analyze_comment(
            comment_raw="x", report_date=date(2026, 1, 1), counterparty_label=None
        )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_openrouter_exhausts_retries_on_429() -> None:
    calls = {"n": 0}

    class FakeResp:
        status = 429

        async def read(self) -> bytes:
            return b""

        async def __aenter__(self) -> FakeResp:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        def post(self, *args: object, **kwargs: object) -> FakeResp:
            calls["n"] += 1
            return FakeResp()

        async def close(self) -> None:
            return None

    client = OpenRouterClient(
        api_key="k",
        base_url="https://example.test/v1",
        model="m",
        timeout_seconds=5,
        max_retries=2,
        session=FakeSession(),  # type: ignore[arg-type]
    )
    with pytest.raises(OpenRouterTransientError):
        await client.analyze_comment(
            comment_raw="x", report_date=date(2026, 1, 1), counterparty_label=None
        )
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_openrouter_invalid_response_envelope_is_schema_error_no_retry() -> None:
    calls = {"n": 0}

    class FakeResp:
        status = 200

        async def json(self, content_type: object = None) -> dict[str, Any]:
            raise json.JSONDecodeError("bad", "not-json", 0)

        async def __aenter__(self) -> FakeResp:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        def post(self, *args: object, **kwargs: object) -> FakeResp:
            calls["n"] += 1
            return FakeResp()

        async def close(self) -> None:
            return None

    client = OpenRouterClient(
        api_key="k",
        base_url="https://example.test/v1",
        model="m",
        timeout_seconds=5,
        max_retries=3,
        session=FakeSession(),  # type: ignore[arg-type]
    )
    with pytest.raises(OpenRouterSchemaError, match="invalid response json"):
        await client.analyze_comment(
            comment_raw="x", report_date=date(2026, 1, 1), counterparty_label=None
        )
    assert calls["n"] == 1
