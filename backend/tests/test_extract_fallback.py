"""Gemini's free tier hits a hard 500-requests/day cap per model, confirmed
live via a real 429. Retrying against that is pointless — it just burns
another of the same exhausted daily requests — so a daily-quota error must
skip straight to the Groq fallback with zero retries. A transient (non-daily)
Gemini 429 should still retry a few times before falling back.
"""
import json

import httpx
import pytest
from groq import RateLimitError as GroqRateLimitError
from openai import RateLimitError as GeminiRateLimitError

from indexer import extract as extract_module
from indexer.github import RawIssue

RAW = RawIssue(
    number=1,
    title="DataFrame.append raises AttributeError",
    body="Calling df.append(row) raises AttributeError on pandas 2.0.",
    url="https://github.com/pandas-dev/pandas/issues/1",
    closer_text="Removed in favor of pd.concat.",
    closer_url="https://github.com/pandas-dev/pandas/pull/2",
    comments=[],
)

# Captured live from a real Gemini 429 against gemini-3.5-flash-lite.
DAILY_QUOTA_BODY = [{
    "error": {
        "code": 429,
        "message": "You exceeded your current quota...",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.Help", "links": []},
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaDimensions": {"location": "global", "model": "gemini-3.5-flash-lite"},
                    "quotaValue": "500",
                }],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
        ],
    }
}]

TRANSIENT_BODY = {"error": {"code": 429, "message": "rate limited", "status": "RESOURCE_EXHAUSTED"}}


def _rate_limit_error(cls, body):
    return cls(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "https://example.com")),
        body=body,
    )


def _openai_style_response(data: dict):
    class _Message:
        content = json.dumps(data)

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    return _Response()


def _client_stub(create_fn):
    calls = {"count": 0}

    class _Completions:
        async def create(self, **kwargs):
            calls["count"] += 1
            return await create_fn(**kwargs)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client(), calls


USEFUL_RESULT = {
    "is_useful": True,
    "problem_summary": "DataFrame.append raises AttributeError.",
    "resolution_summary": "Use pd.concat instead.",
    "error_signature": "AttributeError: 'DataFrame' object has no attribute 'append'",
}


@pytest.mark.asyncio
async def test_daily_quota_exhaustion_falls_back_to_groq_with_zero_retries(monkeypatch):
    async def gemini_create(**kwargs):
        raise _rate_limit_error(GeminiRateLimitError, DAILY_QUOTA_BODY)

    async def groq_create(**kwargs):
        return _openai_style_response(USEFUL_RESULT)

    gemini_client, gemini_calls = _client_stub(gemini_create)
    groq_client, groq_calls = _client_stub(groq_create)

    monkeypatch.setattr(extract_module, "get_extraction_client", lambda: gemini_client)
    monkeypatch.setattr(extract_module, "get_client", lambda: groq_client)
    monkeypatch.setattr(extract_module.asyncio, "sleep", _no_sleep)

    result = await extract_module.extract(RAW)

    assert gemini_calls["count"] == 1  # no wasted retries against an exhausted daily budget
    assert groq_calls["count"] == 1
    assert result.problem_summary == USEFUL_RESULT["problem_summary"]


@pytest.mark.asyncio
async def test_transient_gemini_rate_limit_retries_then_falls_back(monkeypatch):
    async def gemini_create(**kwargs):
        raise _rate_limit_error(GeminiRateLimitError, TRANSIENT_BODY)

    async def groq_create(**kwargs):
        return _openai_style_response(USEFUL_RESULT)

    gemini_client, gemini_calls = _client_stub(gemini_create)
    groq_client, groq_calls = _client_stub(groq_create)

    monkeypatch.setattr(extract_module, "get_extraction_client", lambda: gemini_client)
    monkeypatch.setattr(extract_module, "get_client", lambda: groq_client)
    monkeypatch.setattr(extract_module.asyncio, "sleep", _no_sleep)

    result = await extract_module.extract(RAW)

    assert gemini_calls["count"] == extract_module.MAX_RATE_LIMIT_RETRIES + 1
    assert groq_calls["count"] == 1
    assert result.problem_summary == USEFUL_RESULT["problem_summary"]


@pytest.mark.asyncio
async def test_gemini_success_never_touches_groq(monkeypatch):
    async def gemini_create(**kwargs):
        return _openai_style_response(USEFUL_RESULT)

    async def groq_create(**kwargs):
        raise AssertionError("Groq should not be called when Gemini succeeds")

    gemini_client, gemini_calls = _client_stub(gemini_create)
    groq_client, _ = _client_stub(groq_create)

    monkeypatch.setattr(extract_module, "get_extraction_client", lambda: gemini_client)
    monkeypatch.setattr(extract_module, "get_client", lambda: groq_client)

    result = await extract_module.extract(RAW)

    assert gemini_calls["count"] == 1
    assert result.problem_summary == USEFUL_RESULT["problem_summary"]


@pytest.mark.asyncio
async def test_missing_gemini_key_falls_back_to_groq_instead_of_crashing(monkeypatch):
    # get_extraction_client() raises ValueError when GEMINI_API_KEY is unset
    # (see app/agent/client.py) — a real setup with no Gemini key configured
    # at all, not a rate limit. This must degrade to Groq, not crash the
    # whole indexer run.
    def missing_key():
        raise ValueError("GEMINI_API_KEY is not configured.")

    async def groq_create(**kwargs):
        return _openai_style_response(USEFUL_RESULT)

    groq_client, groq_calls = _client_stub(groq_create)

    monkeypatch.setattr(extract_module, "get_extraction_client", missing_key)
    monkeypatch.setattr(extract_module, "get_client", lambda: groq_client)

    result = await extract_module.extract(RAW)

    assert groq_calls["count"] == 1
    assert result.problem_summary == USEFUL_RESULT["problem_summary"]


@pytest.mark.asyncio
async def test_both_providers_exhausted_returns_none(monkeypatch):
    async def gemini_create(**kwargs):
        raise _rate_limit_error(GeminiRateLimitError, DAILY_QUOTA_BODY)

    async def groq_create(**kwargs):
        raise _rate_limit_error(GroqRateLimitError, TRANSIENT_BODY)

    gemini_client, gemini_calls = _client_stub(gemini_create)
    groq_client, groq_calls = _client_stub(groq_create)

    monkeypatch.setattr(extract_module, "get_extraction_client", lambda: gemini_client)
    monkeypatch.setattr(extract_module, "get_client", lambda: groq_client)
    monkeypatch.setattr(extract_module.asyncio, "sleep", _no_sleep)

    result = await extract_module.extract(RAW)

    assert gemini_calls["count"] == 1
    assert groq_calls["count"] == extract_module.MAX_RATE_LIMIT_RETRIES + 1
    assert result is None


async def _no_sleep(*args, **kwargs):
    return None
