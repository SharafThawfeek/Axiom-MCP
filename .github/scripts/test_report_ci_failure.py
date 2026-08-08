import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from report_ci_failure import format_comment

ANALYSIS = {
    "summary": "DataFrame.append no longer exists.",
    "root_cause": "Removed in pandas 2.0.",
    "explanation": "The method was removed, so the attribute lookup fails.",
    "confidence": "high",
    "next_steps": ["Use pd.concat instead.", "Pin pandas<2.0 if you can't migrate yet."],
}


def test_format_comment_includes_every_field():
    comment = format_comment(ANALYSIS)

    assert ANALYSIS["summary"] in comment
    assert ANALYSIS["root_cause"] in comment
    assert ANALYSIS["explanation"] in comment
    assert ANALYSIS["confidence"] in comment
    for step in ANALYSIS["next_steps"]:
        assert step in comment


def test_missing_api_url_skips_cleanly(tmp_path):
    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "report_ci_failure.py"),
         str(log_file), str(output_file)],
        env={},  # no AXIOM_API_URL
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "not configured" in result.stdout
    assert not output_file.exists()


def test_unreachable_api_skips_without_crashing(tmp_path):
    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "report_ci_failure.py"),
         str(log_file), str(output_file)],
        env={"AXIOM_API_URL": "http://127.0.0.1:1"},  # nothing listens here
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert not output_file.exists()
