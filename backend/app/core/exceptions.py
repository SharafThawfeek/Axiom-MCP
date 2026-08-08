"""Mirrors the exception layer in the shared axiom-ai backend.

This is a seam file, like config.py / database.py / models/base.py: at merge
time the shared repo's version wins and everything importing from here keeps
working unchanged. The base class and both handlers are deliberately
byte-compatible in behaviour with that repo's versions, so merging means
adding the three domain exceptions below to the existing file — not
reconciling two different error conventions.

The one addition is `status_code`. The shared handler hard-codes 400, which
is right for "email already registered" but wrong for "analysis not found".
Defaulting the attribute to 400 means every existing exception there behaves
exactly as it does today, while letting an exception opt into 404.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server error"
        }
    )


class AppException(Exception):
    # Subclasses override to opt out of the default. 400 keeps the shared
    # repo's existing exceptions behaving identically.
    status_code = 400
    message = "Application error"


async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=getattr(exc, "status_code", 400),
        content={
            "error": exc.message
        }
    )


class AnalysisFailed(AppException):
    """The agent couldn't produce an analysis — a provider outage, a rejected
    request, or a log with nothing usable in it."""

    def __init__(self, message: str = "Analysis could not be completed"):
        self.message = message


class InvalidAnalysisId(AppException):
    def __init__(self):
        self.message = "Invalid analysis id"


class AnalysisNotFound(AppException):
    status_code = 404

    def __init__(self):
        self.message = "Analysis not found"
