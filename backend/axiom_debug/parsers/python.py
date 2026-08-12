"""Python traceback parsing.

Shape: frames first, exception last.

    Traceback (most recent call last):
      File "/app/x.py", line 5, in run
        do_thing()
    ValueError: bad input
"""

import re

from axiom_debug.parsers.common import build_signature
from axiom_debug.schemas.analysis import ParsedFailure, StackFrame

LANGUAGE = "python"

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


def is_vendor(path: str) -> bool:
    return any(marker in path for marker in VENDOR_MARKERS)


def library_of(path: str) -> str | None:
    """Best guess at which distribution a vendored frame belongs to."""
    for marker in ("site-packages", "dist-packages"):
        _, sep, tail = path.replace("\\", "/").partition(marker + "/")
        if sep:
            return tail.split("/", 1)[0] or None
    return None


def implicated_library(failure: ParsedFailure) -> str | None:
    """Which package the deepest vendored frame came from.

    Python traces run outermost -> innermost, so the frame nearest the error
    is the LAST one; walk backwards to find it first.
    """
    for frame in reversed(failure.frames):
        if is_vendor(frame.file):
            library = library_of(frame.file)
            if library:
                return library
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
    origin = next((f for f in reversed(frames) if not is_vendor(f.file)), None)
    signature_frame = origin or (frames[-1] if frames else None)

    return (
        ParsedFailure(
            language=LANGUAGE,
            exception_type=exception_type,
            exception_message=message,
            frames=frames,
            signature=build_signature(
                exception_type, message, signature_frame.function if signature_frame else None
            ),
            origin=origin,
        ),
        i,
    )


def parse_lines(lines: list[str]) -> tuple[ParsedFailure | None, int]:
    """Last traceback wins — when exceptions are chained ("during handling of
    the above exception..."), the final one is what actually propagated.

    Returns the failure and the line index where it ended, so the registry
    can compare positions across languages in a mixed log.
    """
    failure = None
    end_index = -1
    i = 0

    while i < len(lines):
        if TRACEBACK_START.match(lines[i]):
            parsed, i = _parse_block(lines, i)
            if parsed and parsed.frames:
                failure, end_index = parsed, i
            continue
        i += 1

    return failure, end_index
