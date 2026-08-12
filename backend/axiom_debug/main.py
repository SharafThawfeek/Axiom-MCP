from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from axiom_debug.api import analysis_router, health_router
from axiom_debug.config import settings
from axiom_debug.core.exceptions import (
    AppException,
    app_exception_handler,
    global_exception_handler,
)
from axiom_debug.core.logger import logger
from axiom_debug.mcp.server import build_http_app

# Built once at import so its lifespan can be chained below. The MCP app owns
# real startup/shutdown work; mounting it without running that lifespan
# produces a route that accepts requests and then hangs.
# Path "/" because this is mounted under "/mcp" below — giving the inner app
# its own "/mcp" prefix too would serve the endpoint at "/mcp/mcp".
mcp_app = build_http_app("/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("%s starting", settings.APP_NAME)
    # Chained, not replaced. FastAPI only runs the lifespan it was given, so
    # a mounted sub-application's lifespan has to be entered explicitly.
    async with mcp_app.lifespan(app):
        yield
    logger.info("%s shutting down", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    lifespan=lifespan,
)

# The playground frontend is browser-based and served from a different
# origin, so without this every request from it fails preflight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered in the same order as the shared backend, so every error
# response across both halves has the same {"error": ...} shape rather than
# this half returning FastAPI's default {"detail": ...}.
app.add_exception_handler(Exception, global_exception_handler)
app.add_exception_handler(AppException, app_exception_handler)

app.include_router(health_router)
app.include_router(analysis_router)

# Streamable HTTP MCP endpoint. Mounted rather than routed because it is a
# complete ASGI application with its own middleware stack — notably the
# Origin validation the spec requires, which must run before anything else
# touches the request.
#
# Note this sits outside the CORSMiddleware policy above on purpose: that
# policy exists for the browser playground, and applying it here would
# advertise the MCP endpoint to browser origins that Origin validation is
# specifically there to keep out.
app.mount("/mcp", mcp_app)


@app.get("/")
async def root():
    return {
        "name": settings.APP_NAME,
        "status": "running",
    }
