"""Portable vector storage and brute-force similarity.

Deliberately not pgvector. Failure memory is per-project and small — a busy
team accumulates hundreds to low thousands of distinct failure signatures,
not millions. At that size an HNSW index is pure overhead: numpy scores a
few thousand 384-dim vectors in well under a millisecond, and the cost is
dominated by loading the rows, not by the arithmetic.

The payoff is that this file has no database-specific code at all. The same
storage format and the same search work on SQLite and Postgres, which is
what makes a zero-infrastructure local install possible. pgvector stays
where it earns its keep: the OSS incident corpus, which is Postgres-only,
optional, and genuinely large.

float32 rather than float64 — bge-small emits float32, so widening would
double the stored bytes to preserve precision the model never produced.
"""

import numpy as np

# Little-endian float32. Pinned explicitly rather than left to native byte
# order: a memory database written on one machine has to be readable on
# another, and ">f4" vs "<f4" silently returns garbage rather than failing.
_DTYPE = np.dtype("<f4")


def encode(vector: list[float]) -> bytes:
    """Pack an embedding for storage in a BLOB / BYTEA column."""
    return np.asarray(vector, dtype=_DTYPE).tobytes()


def decode(blob: bytes) -> np.ndarray:
    """Unpack a stored embedding."""
    return np.frombuffer(blob, dtype=_DTYPE)


def cosine_scores(query: list[float], blobs: list[bytes]) -> np.ndarray:
    """Cosine similarity of `query` against every stored vector, in order.

    Returns a float array in [-1, 1]; empty input returns an empty array
    rather than raising, so callers don't need to special-case a cold index.
    """
    if not blobs:
        return np.empty(0, dtype=_DTYPE)

    matrix = np.frombuffer(b"".join(blobs), dtype=_DTYPE).reshape(len(blobs), -1)
    q = np.asarray(query, dtype=_DTYPE)

    # Guard against a zero vector on either side: np.linalg.norm returns 0
    # and the division would emit a RuntimeWarning and produce nan, which
    # then sorts unpredictably. A zero vector has no direction, so scoring
    # it as 0 similarity is both correct and safe.
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return np.zeros(len(blobs), dtype=_DTYPE)

    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = np.inf

    return (matrix @ q) / (norms * q_norm)
