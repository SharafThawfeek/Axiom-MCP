from groq import AsyncGroq
from openai import AsyncOpenAI

from axiom_debug.config import settings

_client: AsyncGroq | None = None
_extraction_client: AsyncOpenAI | None = None


def get_client() -> AsyncGroq:
    global _client

    if not settings.GROQ_API_KEY:
        raise ValueError(
            "GROQ_API_KEY is not configured. "
            "Set it in backend/.env to enable analysis."
        )

    if _client is None:
        _client = AsyncGroq(
            api_key=settings.GROQ_API_KEY,
            timeout=settings.GROQ_TIMEOUT_SECONDS,
        )

    return _client


def get_extraction_client() -> AsyncOpenAI:
    """Gemini via its OpenAI-compatible endpoint — indexer extraction only."""
    global _extraction_client

    if not settings.GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is not configured. "
            "Set it in backend/.env to enable indexer extraction."
        )

    if _extraction_client is None:
        _extraction_client = AsyncOpenAI(
            api_key=settings.GEMINI_API_KEY,
            base_url=settings.GEMINI_BASE_URL,
            timeout=settings.GEMINI_TIMEOUT_SECONDS,
        )

    return _extraction_client
