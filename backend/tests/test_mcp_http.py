"""The hosted Streamable HTTP surface, mounted inside the FastAPI app.

Exercised through the real ASGI stack with the lifespan running, because
that is where the interesting failure lives: FastAPI only runs the lifespan
it was constructed with, so a mounted MCP app whose lifespan was not chained
mounts fine, accepts requests, and then fails at call time with an
uninitialised session manager. A test that skipped the lifespan would pass
against exactly that broken wiring.
"""

from contextlib import asynccontextmanager

import httpx
import pytest

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_MEMORY_URL", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("AXIOM_PROJECT_ID", "http-test")
    monkeypatch.setenv("AXIOM_ALLOWED_ORIGINS", "https://ok.example.com")
    monkeypatch.delenv("AXIOM_CORPUS_URL", raising=False)
    monkeypatch.delenv("AXIOM_REQUIRE_AUTH", raising=False)

    from axiom_debug.main import app as fastapi_app

    return fastapi_app


@asynccontextmanager
async def serving(app):
    """A client with the app's lifespan actually running.

    A context manager used inside each test rather than an async fixture:
    pytest-asyncio can run generator-fixture teardown in a different task
    from setup, and the MCP session manager's lifespan holds an anyio cancel
    scope that must be exited in the task that entered it. As a fixture this
    passes the assertions and then errors on teardown.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=True
        ) as client:
            yield client


async def test_mcp_endpoint_serves_the_tool_list(app):
    async with serving(app) as http:
        response = await http.post("/mcp/", json=TOOLS_LIST, headers=HEADERS)

    assert response.status_code == 200
    assert "recall_failure_memory" in response.text


async def test_mcp_endpoint_is_reachable_without_the_trailing_slash(app):
    """Clients configure '/mcp'. A 307 preserves method and body, so this works."""
    async with serving(app) as http:
        response = await http.post("/mcp", json=TOOLS_LIST, headers=HEADERS)

    assert response.status_code == 200
    assert "recall_failure_memory" in response.text


async def test_disallowed_browser_origin_is_refused(app):
    """Spec-normative. Without this a web page can drive the server's tools."""
    async with serving(app) as http:
        response = await http.post(
            "/mcp/",
            json=TOOLS_LIST,
            headers={**HEADERS, "Origin": "https://evil.example.com"},
        )

    assert response.status_code == 403
    assert "recall_failure_memory" not in response.text


async def test_configured_origin_is_allowed(app):
    async with serving(app) as http:
        response = await http.post(
            "/mcp/",
            json=TOOLS_LIST,
            headers={**HEADERS, "Origin": "https://ok.example.com"},
        )

    assert response.status_code == 200


async def test_existing_http_api_still_works_alongside_the_mount(app):
    """Mounting MCP must not shadow the app's own routes."""
    async with serving(app) as http:
        response = await http.get("/")

    assert response.status_code == 200
    assert response.json()["status"] == "running"
