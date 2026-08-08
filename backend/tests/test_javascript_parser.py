"""JavaScript/Node stack traces, and the language registry that routes them.

The two formats are inverted relative to each other — Python lists frames
outermost-first with the error last, V8 lists the error first and frames
innermost-first — so "the frame closest to the failure" sits at opposite
ends. Getting that backwards silently blames the wrong file, which is why
origin selection is tested per language rather than assumed shared.
"""

from app.parsers import SUPPORTED_LANGUAGES, implicated_library, parse
from app.parsers.javascript import library_of

NODE_TRACE = """> app@1.0.0 test
TypeError: Cannot read properties of undefined (reading 'name')
    at getUser (/home/runner/work/app/src/users.js:14:22)
    at processRequest (/home/runner/work/app/src/handler.js:31:10)
    at Layer.handle [as handle_request] (/home/runner/work/app/node_modules/express/lib/router/layer.js:95:5)
    at processTicksAndRejections (node:internal/process/task_queues:95:5)
"""

PYTHON_TRACE = """Traceback (most recent call last):
  File "/app/etl.py", line 5, in run
    frame.append(row)
  File "/usr/lib/python3.11/site-packages/pandas/core/generic.py", line 99, in __getattr__
    raise AttributeError(name)
AttributeError: 'DataFrame' object has no attribute 'append'
"""


def test_detects_javascript_without_being_told():
    failure = parse(NODE_TRACE)
    assert failure.language == "javascript"
    assert failure.exception_type == "TypeError"
    assert "Cannot read properties of undefined" in failure.exception_message


def test_origin_is_the_callers_own_code_not_a_dependency():
    # V8 frames run innermost-first, so the first non-vendor frame is the
    # one nearest the failure — the opposite end from Python.
    failure = parse(NODE_TRACE)
    assert failure.origin.file.endswith("src/users.js")
    assert failure.origin.line == 14
    assert failure.origin.function == "getUser"


def test_signature_normalises_volatile_parts():
    failure = parse(NODE_TRACE)
    # The quoted property name varies between runs of the same bug.
    assert "'<str>'" in failure.signature
    assert failure.signature.startswith("TypeError:")
    assert failure.signature.endswith("@ getUser")


def test_implicated_library_finds_the_npm_package():
    assert implicated_library(parse(NODE_TRACE)) == "express"


def test_anonymous_frames_parse():
    trace = """ReferenceError: x is not defined
    at /app/src/boot.js:10:3
"""
    failure = parse(trace)
    assert failure.frames[0].function == "<anonymous>"
    assert failure.frames[0].file == "/app/src/boot.js"


def test_all_vendor_trace_has_no_origin_but_still_parses():
    trace = """ReferenceError: x is not defined
    at /app/node_modules/@babel/core/lib/index.js:10:3
    at node:internal/main/run_main_module:23:47
"""
    failure = parse(trace)
    assert failure.origin is None
    assert implicated_library(failure) == "@babel/core"


def test_ordinary_log_output_is_not_mistaken_for_a_trace():
    # Colons everywhere, no frames — must not produce a phantom failure.
    assert parse("Note: build finished\nWarning: deprecated: use x instead") is None


def test_python_still_parses_and_picks_its_own_origin_end():
    failure = parse(PYTHON_TRACE)
    assert failure.language == "python"
    # Python frames run outermost-first, so the LAST non-vendor frame wins.
    assert failure.origin.file == "/app/etl.py"
    assert implicated_library(failure) == "pandas"


def test_both_languages_are_registered():
    assert set(SUPPORTED_LANGUAGES) == {"python", "javascript"}


class TestLibraryOf:
    def test_plain_package(self):
        assert library_of("/app/node_modules/express/lib/r.js") == "express"

    def test_scoped_package_keeps_the_scope(self):
        # "@babel" alone is a scope, not a package name.
        assert library_of("/app/node_modules/@babel/core/lib/i.js") == "@babel/core"

    def test_nested_dependency_returns_the_innermost(self):
        # a/node_modules/lodash means lodash's code is what's running.
        assert library_of("/app/node_modules/a/node_modules/lodash/x.js") == "lodash"

    def test_windows_separators(self):
        assert library_of(r"C:\app\node_modules\@scope\pkg\i.js") == "@scope/pkg"

    def test_first_party_path_has_no_package(self):
        assert library_of("/app/src/mine.js") is None
