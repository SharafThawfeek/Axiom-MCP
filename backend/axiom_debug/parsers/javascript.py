"""JavaScript / TypeScript (Node, V8) stack trace parsing.

Shape is inverted relative to Python — the error comes FIRST, then frames:

    TypeError: Cannot read properties of undefined (reading 'name')
        at getUser (/app/src/users.js:14:22)
        at /app/node_modules/express/lib/router/index.js:284:15
        at processTicksAndRejections (node:internal/process/task_queues:95:5)

TypeScript needs no separate handling: compiled traces point at .js (or at
.ts when source maps are active), and neither changes the frame syntax.
"""

import re

from axiom_debug.parsers.common import build_signature
from axiom_debug.schemas.analysis import ParsedFailure, StackFrame

LANGUAGE = "javascript"

# "TypeError: msg", "Uncaught ReferenceError: msg", or
# "AssertionError [ERR_ASSERTION]: msg". Deliberately permissive about the
# type name (custom error classes are arbitrary identifiers) — what actually
# confirms this is a stack trace is an `at` frame following it, checked below.
ERROR_HEADER = re.compile(
    r"^\s*(?:Uncaught\s+)?(?P<type>[A-Za-z_$][\w$.]*)"
    r"(?:\s*\[[^\]]+\])?\s*:\s?(?P<message>.*)$"
)

# "at fn (file:line:col)", "at file:line:col", and the variants V8 emits:
# "at async fn (...)", "at new Cls (...)", "at Obj.method [as alias] (...)".
FRAME = re.compile(
    r"^\s*at\s+(?:(?P<function>.+?)\s+\()?"
    r"(?P<file>[^()\s][^()]*?):(?P<line>\d+):(?P<col>\d+)\)?\s*$"
)

# A multi-line error message (assertion diffs, for instance) sits between the
# header and the first frame. Bounded so a header false-positive can't swallow
# an unrelated chunk of build output.
MAX_MESSAGE_CONTINUATION_LINES = 8

VENDOR_PATH_MARKERS = ("node_modules",)

# Node's own builtins surface as "node:internal/..." (modern) or bare
# "internal/..." (older runtimes) — never the caller's code either way.
VENDOR_PATH_PREFIXES = ("node:", "internal/")


def is_vendor(path: str) -> bool:
    normalised = path.replace("\\", "/")
    if normalised.startswith(VENDOR_PATH_PREFIXES):
        return True
    return any(marker in normalised for marker in VENDOR_PATH_MARKERS)


def library_of(path: str) -> str | None:
    """Which npm package a vendored frame belongs to.

    Two things that are easy to get wrong and are handled explicitly:
    scoped packages (`@babel/core` is the name, not `@babel`), and nested
    dependencies — with a/node_modules/b the innermost package is the one
    whose code is actually running, so the LAST marker wins.
    """
    normalised = path.replace("\\", "/")
    marker = "node_modules/"
    index = normalised.rfind(marker)
    if index == -1:
        return None

    parts = [p for p in normalised[index + len(marker):].split("/") if p]
    if not parts:
        return None

    if parts[0].startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    return parts[0]


def implicated_library(failure: ParsedFailure) -> str | None:
    """Which npm package the deepest vendored frame came from.

    V8 traces run innermost -> outermost, the opposite of Python, so the
    frame nearest the error is the FIRST one — walk forwards here.
    """
    for frame in failure.frames:
        if is_vendor(frame.file):
            library = library_of(frame.file)
            if library:
                return library
    return None


def _parse_block(lines: list[str], start: int) -> tuple[ParsedFailure | None, int]:
    header = ERROR_HEADER.match(lines[start])
    if not header:
        return None, start + 1

    exception_type = header.group("type")
    message_parts = [header.group("message") or ""]
    i = start + 1

    # Message continuation, up to the first frame.
    consumed = 0
    while (
        i < len(lines)
        and consumed < MAX_MESSAGE_CONTINUATION_LINES
        and lines[i].strip()
        and not FRAME.match(lines[i])
    ):
        message_parts.append(lines[i].strip())
        i += 1
        consumed += 1

    frames: list[StackFrame] = []
    while i < len(lines):
        match = FRAME.match(lines[i])
        if not match:
            break
        frames.append(
            StackFrame(
                file=match.group("file"),
                line=int(match.group("line")),
                function=(match.group("function") or "<anonymous>").strip(),
                code=None,  # V8 traces carry no source line, unlike Python
            )
        )
        i += 1

    # No frames means this was ordinary log output that happened to contain a
    # colon, not a stack trace.
    if not frames:
        return None, start + 1

    message = " ".join(part for part in message_parts if part).strip()
    origin = next((f for f in frames if not is_vendor(f.file)), None)
    signature_frame = origin or frames[0]

    return (
        ParsedFailure(
            language=LANGUAGE,
            exception_type=exception_type,
            exception_message=message,
            frames=frames,
            signature=build_signature(exception_type, message, signature_frame.function),
            origin=origin,
        ),
        i,
    )


def parse_lines(lines: list[str]) -> tuple[ParsedFailure | None, int]:
    """Last trace wins, matching the Python parser: with a re-thrown or
    wrapped error, the final trace is the one that actually propagated."""
    failure = None
    end_index = -1
    i = 0

    while i < len(lines):
        parsed, next_i = _parse_block(lines, i)
        if parsed:
            failure, end_index = parsed, next_i
        i = next_i

    return failure, end_index
