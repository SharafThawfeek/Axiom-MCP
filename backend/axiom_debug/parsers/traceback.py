"""Extract a structured Python failure from a noisy CI log.

CI logs bury tracebacks in build output, timestamps and colour codes. This
module finds the traceback, parses it, and reduces it to a signature stable
enough to embed.
"""

import re

from axiom_debug.schemas.analysis import ParsedFailure, StackFrame

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# GitHub Actions prefixes every line with an RFC-3339 timestamp.
CI_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")

TRACEBACK_START = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")

FRAME = re.compile(
    r'^\s*File "(?P<file>.+?)", line (?P<line>\d+), in (?P<function>.+?)\s*$'
)

# "ValueError: bad input" or a bare "KeyboardInterrupt".
EXCEPTION = re.compile(
    r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<message>.*))?$"
)

# Frames from these roots are dependency internals, not the caller's code.
VENDOR_MARKERS = (
    "site-packages",
    "dist-packages",
    "/usr/lib/python",
    "\\lib\\python",
    "<frozen ",
)


def _clean(log: str) -> list[str]:
    lines = []
    for raw in log.splitlines():
        line = ANSI.sub("", raw)
        line = CI_TIMESTAMP.sub("", line)
        lines.append(line.rstrip())
    return lines


def _is_vendor(path: str) -> bool:
    return any(marker in path for marker in VENDOR_MARKERS)


def _normalise(message: str) -> str:
    """Strip the parts of a message that vary between otherwise identical runs."""
    text = re.sub(r"0x[0-9a-fA-F]+", "<addr>", message)
    text = re.sub(r"'[^']*'", "'<str>'", text)
    text = re.sub(r'"[^"]*"', '"<str>"', text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return " ".join(text.split())


def _library_of(path: str) -> str | None:
    """Best guess at which distribution a vendored frame belongs to."""
    for marker in ("site-packages", "dist-packages"):
        _, sep, tail = path.replace("\\", "/").partition(marker + "/")
        if sep:
            return tail.split("/", 1)[0] or None
    return None


def _parse_block(lines: list[str], start: int) -> tuple[ParsedFailure | None, int]:
    """Parse one traceback beginning at `lines[start]`.

    Returns the failure and the index just past it.
    """
    frames: list[StackFrame] = []
    i = start + 1

    while i < len(lines):
        match = FRAME.match(lines[i])
        if not match:
            break

        i += 1
        code = None
        # A frame's source line, when present, is indented past the File line.
        if i < len(lines) and lines[i].strip() and lines[i].startswith("    "):
            if not FRAME.match(lines[i]) and not lines[i].lstrip().startswith("^"):
                code = lines[i].strip()
                i += 1
                # Python 3.11+ adds a caret line highlighting the sub-expression.
                while i < len(lines) and lines[i].lstrip().startswith(("^", "~")):
                    i += 1

        frames.append(
            StackFrame(
                file=match.group("file"),
                line=int(match.group("line")),
                function=match.group("function"),
                code=code,
            )
        )

    # Skip blank lines between the last frame and the exception.
    while i < len(lines) and not lines[i].strip():
        i += 1

    if i >= len(lines):
        return None, i

    match = EXCEPTION.match(lines[i].strip())
    if not match:
        return None, i

    exception_type = match.group("type")
    message_parts = [match.group("message") or ""]
    i += 1

    # A message can wrap onto indented continuation lines.
    while i < len(lines) and lines[i].startswith((" ", "\t")) and lines[i].strip():
        message_parts.append(lines[i].strip())
        i += 1

    message = " ".join(part for part in message_parts if part).strip()
    origin = next((f for f in reversed(frames) if not _is_vendor(f.file)), None)

    signature_frame = origin or (frames[-1] if frames else None)
    signature = f"{exception_type}: {_normalise(message)}"
    if signature_frame:
        signature += f" @ {signature_frame.function}"

    return (
        ParsedFailure(
            exception_type=exception_type,
            exception_message=message,
            frames=frames,
            signature=signature,
            origin=origin,
        ),
        i,
    )


def parse(log: str) -> ParsedFailure | None:
    """Return the proximate failure in `log`, or None if there is no traceback.

    When exceptions are chained ("during handling of the above exception..."),
    the last traceback is the one that actually propagated, so it wins.
    """
    lines = _clean(log)
    failure = None
    i = 0

    while i < len(lines):
        if TRACEBACK_START.match(lines[i]):
            parsed, i = _parse_block(lines, i)
            if parsed and parsed.frames:
                failure = parsed
            continue
        i += 1

    return failure


def implicated_library(failure: ParsedFailure) -> str | None:
    """Which third-party package the deepest vendored frame came from, if any."""
    for frame in reversed(failure.frames):
        if _is_vendor(frame.file):
            library = _library_of(frame.file)
            if library:
                return library
    return None
