"""Origin validation for the HTTP transport.

The MCP specification makes this normative — servers MUST validate the
Origin header on every connection — and the reason is specific rather than
generic hygiene.

An MCP server listening on HTTP, especially one bound to localhost, is
reachable from any web page the user happens to have open. The browser will
happily issue a cross-origin POST; without a check, that page can call tools
on an intranet or loopback server that never expected traffic from a
browser at all. Bearer auth does not close this on a local deployment,
because a local deployment typically has no bearer auth. This is the
DNS-rebinding attack class, and the Origin check is the defence.

The policy here is deny-by-default: a request carrying an Origin the server
was not told about is refused. Requests with no Origin header at all are
allowed, because that is what a non-browser client sends — an IDE, a CLI, a
server-to-server call. Browsers always attach Origin on cross-origin
requests, so "absent" reliably means "not a browser", and rejecting it would
break every legitimate client while stopping nothing.
"""

import logging
import os

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("axiom-debug")


def allowed_origins(var: str = "AXIOM_ALLOWED_ORIGINS") -> frozenset[str]:
    """Origins permitted to reach the MCP endpoint from a browser.

    Comma-separated. Empty (the default) means no browser origin is allowed,
    which is the correct default for a server whose clients are IDEs and
    CI runners rather than web pages.
    """
    raw = os.environ.get(var, "")
    return frozenset(o.strip().rstrip("/") for o in raw.split(",") if o.strip())


class OriginValidationMiddleware:
    """Rejects cross-origin browser traffic the deployment didn't opt into."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]):
        self.app = app
        self.allowed = allowed

    @classmethod
    def asgi(cls, allowed: frozenset[str]):
        """Build the (class, kwargs) pair FastMCP's middleware list expects."""
        from starlette.middleware import Middleware

        return Middleware(cls, allowed=allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")

        # No Origin: not a browser. IDEs, CLIs and server-to-server callers
        # never send one, and a browser always does cross-origin.
        if origin is None:
            await self.app(scope, receive, send)
            return

        if origin.rstrip("/") in self.allowed:
            await self.app(scope, receive, send)
            return

        logger.warning("Rejected MCP request from disallowed origin: %s", origin)
        response = PlainTextResponse("Origin not allowed", status_code=403)
        await response(scope, receive, send)
