"""Language registry for failure parsing.

Adding a language means adding a module with the same four names below and
listing it in PARSERS — nothing outside this package changes. The agent,
retrieval, caching and citation layers never learn what language a failure
was in; they only ever see a ParsedFailure.
"""

from axiom_debug.parsers import javascript, python
from axiom_debug.parsers.common import clean_lines
from axiom_debug.schemas.analysis import ParsedFailure

# Order is only a tiebreak for the pathological case of two languages'
# traces ending on the same line; normal selection is by position (below).
PARSERS = (python, javascript)

_BY_LANGUAGE = {module.LANGUAGE: module for module in PARSERS}

SUPPORTED_LANGUAGES = tuple(_BY_LANGUAGE)


def parse(log: str) -> ParsedFailure | None:
    """Return the proximate failure in `log`, or None if there's no trace.

    The language is detected from the log rather than taken from the caller:
    a CI job frequently runs more than one toolchain, and the request field
    is a hint at best. Every parser runs, and the failure that appears
    LATEST in the log wins — the same "last traceback is the one that
    actually propagated" rule the individual parsers use, extended across
    languages so a mixed log resolves the same way a single-language one does.
    """
    lines = clean_lines(log)

    best: ParsedFailure | None = None
    best_position = -1

    for module in PARSERS:
        failure, position = module.parse_lines(lines)
        if failure and position > best_position:
            best, best_position = failure, position

    return best


def implicated_library(failure: ParsedFailure) -> str | None:
    """Which third-party package the failure points at, if any."""
    module = _BY_LANGUAGE.get(failure.language)
    if module is None:
        return None
    return module.implicated_library(failure)


__all__ = ["SUPPORTED_LANGUAGES", "implicated_library", "parse"]
