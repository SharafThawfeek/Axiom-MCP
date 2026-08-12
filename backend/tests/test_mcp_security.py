"""Authentication, tenant scoping, and Origin validation.

These are the tests that matter most if this is ever exposed on a network.
Each one corresponds to a specific way the server could leak one project's
failure history to another, or let a web page drive tools it shouldn't.
"""

import pytest
from axiom_debug.mcp import server as mcp_server
from axiom_debug.mcp.auth import (
    READ_SCOPE,
    WRITE_SCOPE,
    ApiKeyVerifier,
    _digest,
    load_keys_from_env,
    project_id_from_token,
)
from axiom_debug.mcp.origin import allowed_origins

KEY_A = "sk-team-a-secret"
KEY_B = "sk-team-b-secret"


def _key_env(monkeypatch, entries: str) -> None:
    monkeypatch.setenv("AXIOM_API_KEYS", entries)


# --- key loading -----------------------------------------------------------


def test_plaintext_key_is_hashed_on_load(monkeypatch):
    """The record must never retain a usable credential."""
    _key_env(monkeypatch, f'[{{"key": "{KEY_A}", "project_id": "acme/app"}}]')

    (record,) = load_keys_from_env()

    assert record.project_id == "acme/app"
    assert record.digest == _digest(KEY_A)
    assert KEY_A not in record.digest


def test_precomputed_digest_is_accepted(monkeypatch):
    """Real deployments should never put a live key in the environment."""
    _key_env(
        monkeypatch,
        f'[{{"key_sha256": "{_digest(KEY_A)}", "project_id": "acme/app"}}]',
    )

    (record,) = load_keys_from_env()
    assert record.digest == _digest(KEY_A)


def test_keys_default_to_read_only(monkeypatch):
    """Write access has to be granted deliberately."""
    _key_env(monkeypatch, f'[{{"key": "{KEY_A}", "project_id": "acme/app"}}]')

    (record,) = load_keys_from_env()
    assert record.scopes == (READ_SCOPE,)
    assert WRITE_SCOPE not in record.scopes


def test_malformed_key_config_yields_no_keys(monkeypatch):
    """Fail closed. A parse error must not become an open server."""
    _key_env(monkeypatch, "not json at all")
    assert load_keys_from_env() == []

    _key_env(monkeypatch, '{"key": "x", "project_id": "y"}')  # object, not array
    assert load_keys_from_env() == []


def test_entry_without_project_id_is_skipped(monkeypatch):
    """A key with no tenant would be a key to everything."""
    _key_env(
        monkeypatch,
        f'[{{"key": "{KEY_A}"}}, {{"key": "{KEY_B}", "project_id": "acme/api"}}]',
    )

    records = load_keys_from_env()
    assert [r.project_id for r in records] == ["acme/api"]


def test_no_key_config_yields_no_keys(monkeypatch):
    monkeypatch.delenv("AXIOM_API_KEYS", raising=False)
    assert load_keys_from_env() == []


# --- verification ----------------------------------------------------------


async def test_valid_token_resolves_to_its_own_project():
    # Records built directly rather than through the env loader, so a
    # regression in loading can't make this pass or fail for the wrong reason.
    from axiom_debug.mcp.auth import ApiKeyRecord

    verifier = ApiKeyVerifier(
        [
            ApiKeyRecord(_digest(KEY_A), "acme/app", (READ_SCOPE,)),
            ApiKeyRecord(_digest(KEY_B), "acme/api", (READ_SCOPE, WRITE_SCOPE)),
        ]
    )

    token = await verifier.verify_token(KEY_A)
    assert token is not None
    assert project_id_from_token(token) == "acme/app"
    assert token.scopes == [READ_SCOPE]

    other = await verifier.verify_token(KEY_B)
    assert project_id_from_token(other) == "acme/api"
    assert WRITE_SCOPE in other.scopes


async def test_unknown_token_is_rejected():
    from axiom_debug.mcp.auth import ApiKeyRecord

    verifier = ApiKeyVerifier([ApiKeyRecord(_digest(KEY_A), "acme/app", (READ_SCOPE,))])

    assert await verifier.verify_token("sk-not-a-real-key") is None
    assert await verifier.verify_token("") is None


async def test_verifier_with_no_keys_rejects_everything():
    verifier = ApiKeyVerifier([])
    assert await verifier.verify_token(KEY_A) is None


def test_project_id_from_missing_token_is_none():
    assert project_id_from_token(None) is None


# --- tenant resolution -----------------------------------------------------


def test_auth_mode_refuses_to_fall_back_to_the_local_project(monkeypatch):
    """The most dangerous possible bug in this codebase.

    If an unauthenticated request silently resolved to whatever checkout the
    server process sits in, a caller with no credentials would read that
    project's entire failure history. It must raise instead.
    """
    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("AXIOM_REQUIRE_AUTH", "true")
    monkeypatch.setenv("AXIOM_PROJECT_ID", "local-fallback-project")
    mcp_server.reset_state()

    try:
        with pytest.raises(ToolError) as exc:
            mcp_server._project_id()
        assert "unauthenticated" in str(exc.value).lower()
    finally:
        mcp_server.reset_state()


def test_local_mode_uses_the_pinned_project(monkeypatch):
    monkeypatch.delenv("AXIOM_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("AXIOM_PROJECT_ID", "pinned")
    mcp_server.reset_state()

    try:
        assert mcp_server._project_id() == "pinned"
    finally:
        mcp_server.reset_state()


# --- origin validation -----------------------------------------------------


def test_no_allowed_origins_by_default(monkeypatch):
    monkeypatch.delenv("AXIOM_ALLOWED_ORIGINS", raising=False)
    assert allowed_origins() == frozenset()


def test_allowed_origins_are_parsed_and_normalised(monkeypatch):
    monkeypatch.setenv(
        "AXIOM_ALLOWED_ORIGINS", "https://app.example.com/, http://localhost:3000"
    )
    assert allowed_origins() == {"https://app.example.com", "http://localhost:3000"}


class _Recorder:
    """Minimal downstream ASGI app that records whether it was reached."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _run(middleware, headers: list[tuple[bytes, bytes]]):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware({"type": "http", "headers": headers, "method": "POST"}, receive, send)
    return sent


async def test_request_without_origin_is_allowed():
    """Non-browser clients — IDEs, CLIs, CI — never send Origin."""
    from axiom_debug.mcp.origin import OriginValidationMiddleware

    downstream = _Recorder()
    mw = OriginValidationMiddleware(downstream, frozenset())

    await _run(mw, [])
    assert downstream.called is True


async def test_disallowed_origin_is_rejected():
    """The DNS-rebinding defence. A web page must not drive local tools."""
    from axiom_debug.mcp.origin import OriginValidationMiddleware

    downstream = _Recorder()
    mw = OriginValidationMiddleware(downstream, frozenset({"https://ok.example.com"}))

    sent = await _run(mw, [(b"origin", b"https://evil.example.com")])

    assert downstream.called is False
    assert sent[0]["status"] == 403


async def test_allowed_origin_passes_through():
    from axiom_debug.mcp.origin import OriginValidationMiddleware

    downstream = _Recorder()
    mw = OriginValidationMiddleware(downstream, frozenset({"https://ok.example.com"}))

    await _run(mw, [(b"origin", b"https://ok.example.com")])
    assert downstream.called is True
