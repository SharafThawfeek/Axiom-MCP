"""Runs as a CI step, only on failure. Sends the failed job's log to Axiom
Debug's /analyze endpoint and writes a markdown comment for the next step to
post on the PR — this is the "Manual mode" delivery path: the developer gets
told what broke and why, but nothing gets fixed or pushed automatically.

Deliberately stdlib-only (no httpx/requests) — this runs in its own CI job,
not inside the backend's installed environment, so it has no dependencies to
install.

    python report_ci_failure.py <log_file> <output_file>
"""

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 90


def format_comment(analysis: dict) -> str:
    next_steps = "\n".join(f"- {step}" for step in analysis["next_steps"])
    return (
        "### 🔍 Axiom Debug\n\n"
        f"**{analysis['summary']}**\n\n"
        f"**Root cause:** {analysis['root_cause']}\n\n"
        f"{analysis['explanation']}\n\n"
        f"**Next steps:**\n{next_steps}\n\n"
        f"*Confidence: {analysis['confidence']}*"
    )


def analyze(api_url: str, log_text: str) -> dict:
    payload = json.dumps({"log": log_text}).encode("utf-8")
    request = urllib.request.Request(
        api_url.rstrip("/") + "/analyze",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: report_ci_failure.py <log_file> <output_file>", file=sys.stderr)
        return 1

    log_path, output_path = sys.argv[1], sys.argv[2]

    api_url = os.environ.get("AXIOM_API_URL", "").strip()
    if not api_url:
        print(
            "AXIOM_API_URL is not configured — skipping analysis. "
            "Set it as a repo variable to enable this step."
        )
        return 0

    with open(log_path, encoding="utf-8", errors="replace") as f:
        log_text = f.read()

    try:
        result = analyze(api_url, log_text)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Axiom Debug was unreachable ({exc}); skipping comment.", file=sys.stderr)
        return 0

    comment = format_comment(result["analysis"])
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(comment)

    print(f"Wrote comment to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
