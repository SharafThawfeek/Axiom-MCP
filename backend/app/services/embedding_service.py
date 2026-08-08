"""Turns text into vectors, locally, for free.

Runs BAAI/bge-small-en-v1.5 through fastembed's ONNX runtime — no PyTorch,
no API key, no network call. This has to be the same model at index time and
query time, or similarity search compares vectors from different spaces and
returns noise. If EMBEDDING_MODEL or EMBEDDING_DIM in config.py ever change,
the whole index needs re-embedding — see migrations/versions for where the
384 dimension is baked into the schema.
"""

import asyncio
import threading

from fastembed import TextEmbedding

from app.config import settings

_model: TextEmbedding | None = None

# aembed_one dispatches to a worker thread, so a burst of concurrent cold
# requests hits _get_model() from several threads at once. Without this
# lock the `if _model is None` check races: measured live, 8 concurrent
# cold embeds constructed 8 separate models — ~130MB of ONNX weights each,
# a real memory spike and 3.25s of redundant loading for work that should
# happen once. Double-checked locking: the fast path stays lock-free once
# the model exists, so the steady state costs nothing.
_model_lock = threading.Lock()


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = TextEmbedding(model_name=settings.EMBEDDING_MODEL)
    return _model


class EmbeddingService:

    @staticmethod
    def embed_one(text: str) -> list[float]:
        """Synchronous. Only call from sync code (the indexer) — see aembed_one."""
        return next(_get_model().embed([text])).tolist()

    @staticmethod
    def embed_many(texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in _get_model().embed(texts)]

    @staticmethod
    async def aembed_one(text: str) -> list[float]:
        """Async-safe version for the request path.

        ONNX inference is CPU-bound and blocks whatever thread it runs on
        (~4ms measured). Calling the sync version directly from a coroutine
        stalls the whole event loop, serialising every concurrent request
        behind it, so the request path goes through a worker thread instead.
        """
        return await asyncio.to_thread(EmbeddingService.embed_one, text)
