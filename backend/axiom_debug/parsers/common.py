"""Pieces every language parser needs, regardless of trace format.

CI log noise (colour codes, runner timestamps) and signature normalisation
are identical whatever produced the failure — only the shape of a stack
trace is language-specific, so only that lives in the per-language modules.
"""

import re

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# GitHub Actions prefixes every line with an RFC-3339 timestamp.
CI_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")


def clean_lines(log: str) -> list[str]:
    lines = []
    for raw in log.splitlines():
        line = ANSI.sub("", raw)
        line = CI_TIMESTAMP.sub("", line)
        lines.append(line.rstrip())
    return lines


# A bare identifier, optionally dotted for a qualified name (pandas.DataFrame,
# module.func). Deliberately conservative: only letters, digits and
# underscores past the first character, no path separators, no spaces, no
# punctuation. This is what separates a class or attribute name — the actual
# identity of an AttributeError/NameError/KeyError — from incidental data
# (a file path, a UUID, a piece of user input) that legitimately varies
# between two runs of the same bug.
_IDENTIFIER_TOKEN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_MAX_IDENTIFIER_LEN = 80


def _redact_quoted(match: re.Match) -> str:
    quote, inner = match.group(1), match.group(2)
    if len(inner) <= _MAX_IDENTIFIER_LEN and _IDENTIFIER_TOKEN.match(inner):
        # Keep it. "AttributeError: 'DataFrame' object has no attribute
        # 'append'" and "...'ItemCollection'... 'dedupe_by_key'" are
        # different bugs — blanking both quoted spans to the same '<str>'
        # placeholder made them collide onto one signature, which meant one
        # failure's recorded resolution silently overwrote the other's.
        return match.group(0)
    return f"{quote}<str>{quote}"


def normalise_message(message: str) -> str:
    """Strip the parts of a message that vary between otherwise identical runs.

    This is what makes the failure signature stable enough to use as a cache
    key and to embed — two runs of the same bug differ in memory addresses,
    quoted data values and line counts, but not in what actually broke. An
    identifier-shaped quoted token (a class name, an attribute name, a
    module path) is not incidental in that sense — it IS what broke, so it
    survives; only quoted content that doesn't look like an identifier gets
    collapsed to a placeholder.
    """
    text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", message)
    text = re.sub(r"(')([^']*)\1", _redact_quoted, text)
    text = re.sub(r'(")([^"]*)\1', _redact_quoted, text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return " ".join(text.split())


def build_signature(exception_type: str, message: str, function: str | None) -> str:
    signature = f"{exception_type}: {normalise_message(message)}"
    if function:
        signature += f" @ {function}"
    return signature
