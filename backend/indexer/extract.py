"""Turns one raw GitHub issue into a clean (problem, resolution) pair.

Runs against every closed issue in the crawl — thousands of calls — so this
tries Gemini (EXTRACTION_MODEL) first, then falls back to Groq's cheap
VERIFIER_MODEL. Two independent free-tier quotas beat one: Gemini's
per-minute throughput is strong but confirmed live to hit a hard
RESOURCE_EXHAUSTED wall at 500 requests/day per model
(GenerateRequestsPerDayPerProjectPerModel-FreeTier); Groq's tighter
constraint is per-minute TPM instead. When Gemini's error is specifically
that daily-quota exhaustion, retrying is pointless — every retry just burns
one more of the same already-empty daily budget — so that case skips
straight to the Groq fallback instead of spinning through
MAX_RATE_LIMIT_RETRIES first.
"""

import asyncio
import json

from groq import RateLimitError as GroqRateLimitError
from openai import RateLimitError as GeminiRateLimitError

from app.agent.client import get_client, get_extraction_client
from app.config import settings
from app.core.logger import logger
from indexer.github import RawIssue

MAX_RATE_LIMIT_RETRIES = 6
DEFAULT_RETRY_SECONDS = 10.0

EXTRACT_PROMPT = """You turn a closed GitHub issue into a clean, indexable
record of a real bug and its real fix. You will see the issue title, body,
what closed it (a commit message or PR title), and a few comments.

Reject anything that isn't a genuine bug report with a code-level fix:
feature requests, questions, documentation issues, "+1" threads, duplicates,
and issues closed as "not planned" or "won't fix" are not useful here —
mark them not useful rather than forcing a summary.

For genuine bugs: write problem_summary as what the user hit, in the voice
of an error report — not the issue title verbatim. Write resolution_summary
as what actually fixed it, grounded in the closing commit/PR, not guessed.
If the failure clearly involves a specific exception type, extract it as
error_signature in the form "ExceptionType: message pattern" — otherwise
leave it null."""

EXTRACT_SCHEMA = {
    "name": "issue_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "is_useful": {
                "type": "boolean",
                "description": "True only for a genuine bug report with a real code-level fix.",
            },
            "problem_summary": {
                "type": ["string", "null"],
                "description": "What the user hit. Null if not useful.",
            },
            "resolution_summary": {
                "type": ["string", "null"],
                "description": "What actually fixed it. Null if not useful.",
            },
            "error_signature": {
                "type": ["string", "null"],
                "description": '"ExceptionType: message pattern", or null if not exception-shaped.',
            },
        },
        "required": ["is_useful", "problem_summary", "resolution_summary", "error_signature"],
        "additionalProperties": False,
    },
}


class ExtractedIncident:
    def __init__(
        self,
        problem_summary: str,
        resolution_summary: str,
        error_signature: str | None,
        raw: RawIssue,
    ):
        self.problem_summary = problem_summary
        self.resolution_summary = resolution_summary
        self.error_signature = error_signature
        self.raw = raw


def _build_context(raw: RawIssue) -> str:
    return (
        f"Title: {raw.title}\n\n"
        f"Body:\n{raw.body[:4000]}\n\n"
        f"Closed by: {raw.closer_text or '(unknown)'}\n\n"
        f"Comments:\n" + "\n---\n".join(c[:1000] for c in raw.comments[:5])
    )


def _is_daily_quota_exhausted(exc: GeminiRateLimitError) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        return False

    for detail in body.get("error", {}).get("details", []):
        for violation in detail.get("violations", []):
            if "PerDay" in violation.get("quotaId", ""):
                return True
    return False


def _retry_wait_seconds(exc) -> float:
    try:
        return float(exc.response.headers.get("retry-after", DEFAULT_RETRY_SECONDS))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_RETRY_SECONDS


async def _extract_gemini(raw: RawIssue, context: str) -> dict | None:
    """Returns parsed JSON on success, None to signal "fall back to Groq"."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            client = get_extraction_client()
            response = await client.chat.completions.create(
                model=settings.EXTRACTION_MODEL,
                messages=[
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": context},
                ],
                response_format={"type": "json_schema", "json_schema": EXTRACT_SCHEMA},
            )
            text = response.choices[0].message.content
            return json.loads(text) if text else None
        except GeminiRateLimitError as exc:
            if _is_daily_quota_exhausted(exc):
                logger.info(
                    "Gemini daily quota exhausted; falling back to Groq for issue #%d",
                    raw.number,
                )
                return None

            if attempt == MAX_RATE_LIMIT_RETRIES:
                logger.info(
                    "Gemini still rate-limited after %d retries; falling back to Groq for issue #%d",
                    MAX_RATE_LIMIT_RETRIES, raw.number,
                )
                return None

            wait = _retry_wait_seconds(exc)
            logger.info(
                "Issue #%d rate-limited on Gemini, waiting %.1fs (attempt %d/%d)",
                raw.number, wait, attempt + 1, MAX_RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.warning(
                "Gemini extraction failed for issue #%d: %s; falling back to Groq",
                raw.number, exc,
            )
            return None

    return None


async def _extract_groq(raw: RawIssue, context: str) -> dict | None:
    client = get_client()

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            response = await client.chat.completions.create(
                model=settings.VERIFIER_MODEL,
                messages=[
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": context},
                ],
                response_format={"type": "json_schema", "json_schema": EXTRACT_SCHEMA},
            )
            text = response.choices[0].message.content
            return json.loads(text) if text else None
        except GroqRateLimitError as exc:
            if attempt == MAX_RATE_LIMIT_RETRIES:
                logger.warning(
                    "Issue #%d still rate-limited on Groq fallback after %d retries; giving up",
                    raw.number, MAX_RATE_LIMIT_RETRIES,
                )
                return None

            wait = _retry_wait_seconds(exc)
            logger.info(
                "Issue #%d rate-limited on Groq fallback, waiting %.1fs (attempt %d/%d)",
                raw.number, wait, attempt + 1, MAX_RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.warning("Groq fallback extraction failed for issue #%d: %s", raw.number, exc)
            return None

    return None


async def extract(raw: RawIssue) -> ExtractedIncident | None:
    context = _build_context(raw)

    data = await _extract_gemini(raw, context)
    if data is None:
        data = await _extract_groq(raw, context)

    if data is None or not data["is_useful"]:
        return None

    return ExtractedIncident(
        problem_summary=data["problem_summary"],
        resolution_summary=data["resolution_summary"],
        error_signature=data["error_signature"],
        raw=raw,
    )
