"""Failure-memory storage, recall tiers, and the tenant boundary."""

import numpy as np
import pytest
from axiom_debug.memory import MemoryStore, vectors
from axiom_debug.memory.project import normalise_remote
from axiom_debug.services.embedding_service import EmbeddingService

SIG_A = "AttributeError: 'DataFrame' object has no attribute '<str>' @ transform"
SIG_B = "ModuleNotFoundError: No module named '<str>' @ <module>"


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Deterministic stand-in for bge-small.

    The real model is a 130MB download and ~4ms per call; neither is wanted
    in a unit test. What matters here is the storage and ranking logic, so a
    hash-seeded vector that is stable per input and different across inputs
    exercises exactly the same code paths.
    """

    async def _embed(text: str) -> list[float]:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        vec = rng.standard_normal(384).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()

    monkeypatch.setattr(EmbeddingService, "aembed_one", staticmethod(_embed))
    return _embed


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}")
    await s.initialise()
    yield s
    await s.close()


async def test_record_then_recall_exact_signature(store, fake_embeddings):
    await store.record(
        project_id="proj-1",
        signature=SIG_A,
        resolution="Use pd.concat instead; .append was removed in pandas 2.0.",
        resolved_by="https://github.com/acme/app/pull/412",
    )

    hits = await store.recall(project_id="proj-1", signature=SIG_A)

    assert len(hits) == 1
    assert hits[0].exact is True
    assert hits[0].similarity == 1.0
    assert "pd.concat" in hits[0].memory.resolution
    assert hits[0].memory.resolved_by.endswith("/412")


async def test_exact_recall_does_not_embed(store, fake_embeddings, monkeypatch):
    """Tier 1 must not pay for the model.

    This is the efficiency claim the whole design rests on, so it is asserted
    rather than assumed: if an exact hit ever starts embedding, this fails.
    """
    await store.record(project_id="proj-1", signature=SIG_A, resolution="fixed")

    async def _explode(text: str):
        raise AssertionError("tier 1 must not call the embedding model")

    monkeypatch.setattr(EmbeddingService, "aembed_one", staticmethod(_explode))

    hits = await store.recall(project_id="proj-1", signature=SIG_A, limit=1)
    assert len(hits) == 1
    assert hits[0].exact is True


async def test_recall_is_scoped_to_one_project(store, fake_embeddings):
    """The tenant boundary. A leak here exposes another team's history."""
    await store.record(project_id="proj-1", signature=SIG_A, resolution="team one fix")
    await store.record(project_id="proj-2", signature=SIG_A, resolution="team two fix")

    hits = await store.recall(project_id="proj-2", signature=SIG_A)

    assert len(hits) == 1
    assert hits[0].memory.resolution == "team two fix"
    assert await store.count("proj-1") == 1
    assert await store.count("proj-2") == 1


async def test_unknown_project_recalls_nothing(store, fake_embeddings):
    await store.record(project_id="proj-1", signature=SIG_A, resolution="fix")

    assert await store.recall(project_id="proj-unknown", signature=SIG_A) == []


async def test_repeat_failure_increments_rather_than_duplicating(store, fake_embeddings):
    await store.record(project_id="p", signature=SIG_A, resolution="first")
    await store.record(project_id="p", signature=SIG_A, resolution="better fix")

    assert await store.count("p") == 1
    hits = await store.recall(project_id="p", signature=SIG_A)
    assert hits[0].memory.occurrences == 2
    assert hits[0].memory.resolution == "better fix"


async def test_unrelated_signature_is_not_recalled(store, fake_embeddings):
    """An empty answer is correct. Returning the least-bad row is not."""
    await store.record(project_id="p", signature=SIG_A, resolution="fix")

    assert await store.recall(project_id="p", signature=SIG_B) == []


async def test_recall_with_no_signature_and_no_text_returns_empty(store, fake_embeddings):
    await store.record(project_id="p", signature=SIG_A, resolution="fix")

    assert await store.recall(project_id="p") == []


# --- vectors ---------------------------------------------------------------


def test_vector_roundtrip_preserves_values():
    original = [0.5, -0.25, 0.125]
    assert list(vectors.decode(vectors.encode(original))) == pytest.approx(original)


def test_cosine_identical_vector_scores_one():
    v = [1.0, 0.0, 0.0]
    assert vectors.cosine_scores(v, [vectors.encode(v)])[0] == pytest.approx(1.0)


def test_cosine_orthogonal_vector_scores_zero():
    scores = vectors.cosine_scores([1.0, 0.0], [vectors.encode([0.0, 1.0])])
    assert scores[0] == pytest.approx(0.0)


def test_cosine_handles_empty_and_zero_vectors():
    """A cold index and a degenerate vector must not raise or produce nan."""
    assert len(vectors.cosine_scores([1.0, 0.0], [])) == 0

    scores = vectors.cosine_scores([0.0, 0.0], [vectors.encode([1.0, 0.0])])
    assert scores[0] == 0.0
    assert not np.isnan(scores).any()

    scores = vectors.cosine_scores([1.0, 0.0], [vectors.encode([0.0, 0.0])])
    assert not np.isnan(scores).any()


# --- project identity ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Acme/App.git",
        "https://github.com/Acme/App.git",
        "https://github.com/acme/app",
        "ssh://git@github.com/Acme/App.git",
    ],
)
def test_remote_urls_for_one_repo_normalise_together(url):
    """SSH and HTTPS clones of the same repo must share one memory."""
    assert normalise_remote(url) == "github.com/acme/app"


def test_different_repos_do_not_collide():
    assert normalise_remote("git@github.com:acme/app.git") != normalise_remote(
        "git@github.com:acme/other.git"
    )
