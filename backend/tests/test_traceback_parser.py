from app.parsers import implicated_library, parse

SIMPLE = """
Traceback (most recent call last):
  File "/app/main.py", line 42, in <module>
    result = process(data)
  File "/app/utils.py", line 17, in process
    return frame.append(row)
AttributeError: 'DataFrame' object has no attribute 'append'
"""

CI_NOISE = """
2026-08-07T12:00:01.1234567Z ##[group]Run pytest
2026-08-07T12:00:02.0000000Z \x1b[31mFAILED\x1b[0m tests/test_api.py
2026-08-07T12:00:03.0000000Z Traceback (most recent call last):
2026-08-07T12:00:03.1000000Z   File "/app/api.py", line 8, in handler
2026-08-07T12:00:03.2000000Z     return client.request(url)
2026-08-07T12:00:03.3000000Z   File "/usr/lib/python3.12/site-packages/requests/api.py", line 59, in request
2026-08-07T12:00:03.4000000Z     raise ConnectionError(msg)
2026-08-07T12:00:03.5000000Z requests.exceptions.ConnectionError: Max retries exceeded with url: /v1/data
"""

CHAINED = """
Traceback (most recent call last):
  File "/app/load.py", line 3, in load
    return json.loads(raw)
ValueError: Expecting value: line 1 column 1 (char 0)

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/main.py", line 11, in <module>
    config = load()
  File "/app/load.py", line 5, in load
    raise ConfigError("config.json is not valid JSON")
ConfigError: config.json is not valid JSON
"""


def test_parses_simple_traceback():
    failure = parse(SIMPLE)

    assert failure is not None
    assert failure.exception_type == "AttributeError"
    assert failure.exception_message == "'DataFrame' object has no attribute 'append'"
    assert len(failure.frames) == 2
    assert failure.frames[0].file == "/app/main.py"
    assert failure.frames[0].line == 42
    assert failure.frames[1].function == "process"
    assert failure.frames[1].code == "return frame.append(row)"


def test_signature_normalises_volatile_parts():
    failure = parse(SIMPLE)

    assert "'<str>'" in failure.signature
    assert "DataFrame" not in failure.signature
    assert failure.signature.startswith("AttributeError:")


def test_strips_ci_timestamps_and_ansi_codes():
    failure = parse(CI_NOISE)

    assert failure is not None
    assert failure.exception_type == "requests.exceptions.ConnectionError"
    assert len(failure.frames) == 2
    assert failure.frames[0].file == "/app/api.py"


def test_origin_skips_vendored_frames():
    failure = parse(CI_NOISE)

    assert failure.origin is not None
    assert failure.origin.file == "/app/api.py"
    assert failure.origin.function == "handler"


def test_implicated_library_reads_site_packages_path():
    failure = parse(CI_NOISE)

    assert implicated_library(failure) == "requests"


def test_chained_exception_returns_the_proximate_one():
    failure = parse(CHAINED)

    assert failure is not None
    assert failure.exception_type == "ConfigError"
    assert failure.exception_message == "config.json is not valid JSON"


def test_returns_none_when_there_is_no_traceback():
    assert parse("npm ERR! code ELIFECYCLE\nnpm ERR! errno 1") is None
