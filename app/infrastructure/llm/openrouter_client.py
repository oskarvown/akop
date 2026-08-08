"""OpenRouter client for Stage 4.4 comment enrichment (structured facts only)."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import aiohttp

logger = logging.getLogger(__name__)


class OpenRouterTransientError(RuntimeError):
    """Timeout / 5xx / rate limit — do not persist as final unparsed."""


class OpenRouterSchemaError(ValueError):
    """Model returned JSON that fails the fact schema."""


@dataclass(frozen=True)
class LlmCommentFacts:
    mentioned_date: date | None
    mentioned_amount: Decimal | None
    action: str | None
    reason: str | None
    responsible_person: str | None
    summary: str | None
    confidence: str
    raw_json: dict[str, Any]


class CommentLlmClient(Protocol):
    async def analyze_comment(
        self,
        *,
        comment_raw: str,
        report_date: date,
        counterparty_label: str | None,
    ) -> LlmCommentFacts: ...


_SYSTEM = (
    "Extract structured facts from a Russian AR manager comment. "
    "Return ONLY JSON with keys: mentioned_date (YYYY-MM-DD or null), "
    "mentioned_amount (string decimal or null), action, reason, "
    "responsible_person, summary, confidence (high|medium|low|none). "
    "Do not judge promise fulfillment, overdue, or broken status."
)


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenRouterSchemaError("invalid string field")
    text = value.strip()
    return text or None


def _parse_facts(payload: dict[str, Any]) -> LlmCommentFacts:
    """Normalize all payload validation failures to OpenRouterSchemaError."""
    try:
        if not isinstance(payload, dict):
            raise OpenRouterSchemaError("content not object")
        required = {
            "mentioned_date",
            "mentioned_amount",
            "action",
            "reason",
            "responsible_person",
            "summary",
            "confidence",
        }
        if not required.issubset(payload.keys()):
            raise OpenRouterSchemaError("missing keys")
        mentioned_date = None
        raw_date = payload.get("mentioned_date")
        if raw_date not in (None, ""):
            try:
                mentioned_date = date.fromisoformat(str(raw_date))
            except ValueError as exc:
                raise OpenRouterSchemaError("invalid date") from exc
        mentioned_amount = None
        raw_amount = payload.get("mentioned_amount")
        if raw_amount not in (None, ""):
            try:
                mentioned_amount = Decimal(str(raw_amount))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise OpenRouterSchemaError("invalid amount") from exc
        confidence = payload.get("confidence")
        if confidence is None:
            confidence = "none"
        if not isinstance(confidence, str):
            raise OpenRouterSchemaError("invalid confidence")
        confidence = confidence.strip() or "none"
        if confidence not in {"high", "medium", "low", "none"}:
            raise OpenRouterSchemaError("invalid confidence")
        return LlmCommentFacts(
            mentioned_date=mentioned_date,
            mentioned_amount=mentioned_amount,
            action=_opt_str(payload.get("action")),
            reason=_opt_str(payload.get("reason")),
            responsible_person=_opt_str(payload.get("responsible_person")),
            summary=_opt_str(payload.get("summary")),
            confidence=confidence,
            raw_json=payload,
        )
    except OpenRouterSchemaError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise OpenRouterSchemaError("invalid payload") from exc


def _extract_message_content(data: Any) -> str:
    try:
        if not isinstance(data, dict):
            raise OpenRouterSchemaError("response not object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenRouterSchemaError("missing choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise OpenRouterSchemaError("invalid choices")
        message = first.get("message")
        if not isinstance(message, dict):
            raise OpenRouterSchemaError("invalid message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterSchemaError("empty content")
        return content
    except OpenRouterSchemaError:
        raise
    except (TypeError, AttributeError, KeyError, IndexError) as exc:
        raise OpenRouterSchemaError("invalid choices") from exc


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int = 2,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._session = session

    async def analyze_comment(
        self,
        *,
        comment_raw: str,
        report_date: date,
        counterparty_label: str | None,
    ) -> LlmCommentFacts:
        # Caller must pass already-redacted comment/label; never log raw payload.
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "report_date": report_date.isoformat(),
                            "counterparty_label": counterparty_label,
                            "comment": comment_raw,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"
        last_error: Exception | None = None
        owns_session = self._session is None
        session = self._session or aiohttp.ClientSession()
        attempts = max(1, int(self._max_retries) + 1)
        try:
            for attempt in range(attempts):
                try:
                    timeout = aiohttp.ClientTimeout(total=self._timeout)
                    async with session.post(
                        url, json=body, headers=headers, timeout=timeout
                    ) as resp:
                        if resp.status in {408, 429} or resp.status >= 500:
                            await resp.read()
                            raise OpenRouterTransientError(
                                f"openrouter_http_{resp.status}"
                            )
                        if resp.status >= 400:
                            await resp.read()
                            raise OpenRouterSchemaError(
                                f"openrouter_http_{resp.status}"
                            )
                        data = await resp.json(content_type=None)
                    content = _extract_message_content(data)
                    try:
                        payload = json.loads(content)
                    except json.JSONDecodeError as exc:
                        raise OpenRouterSchemaError("invalid json") from exc
                    return _parse_facts(payload)
                except OpenRouterSchemaError:
                    raise
                except OpenRouterTransientError as exc:
                    last_error = exc
                    if attempt + 1 >= attempts:
                        raise
                    continue
                except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                    last_error = OpenRouterTransientError(type(exc).__name__)
                    if attempt + 1 >= attempts:
                        raise last_error from exc
                    continue
            assert last_error is not None
            raise last_error
        finally:
            if owns_session:
                await session.close()
