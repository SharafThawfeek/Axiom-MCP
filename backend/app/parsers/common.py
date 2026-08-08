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


def normalise_message(message: str) -> str:
    """Strip the parts of a message that vary between otherwise identical runs.

    This is what makes the failure signature stable enough to use as a cache
    key and to embed — two runs of the same bug differ in memory addresses,
    quoted values and line counts, but not in what actually broke.
    """
    text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", message)
    text = re.sub(r"'[^']*'", "'<str>'", text)
    text = re.sub(r'"[^"]*"', '"<str>"', text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return " ".join(text.split())


def build_signature(exception_type: str, message: str, function: str | None) -> str:
    signature = f"{exception_type}: {normalise_message(message)}"
    if function:
        signature += f" @ {function}"
    return signature
