"""The error-message rule for user-facing failures.

* No generic message without an error code: "this opportunity is not
  available, try again" hides every cause.
* A 422 names the field that is wrong and says what format is expected.
* An unknown error shows the HTTP status.

Used by the browser-smoke verifier on the message an invalid submit shows; the
same rule is in the common prompt for the agent.
"""

from __future__ import annotations

import re

GENERIC = ("try again", "not available", "something went wrong", "an error occurred",
           "error occurred", "unexpected error", "unknown error", "bad request",
           "invalid request", "request failed", "failed to", "oops")
FORMAT_HINT_RE = re.compile(r"\b(?:format|e\.g|example|must|should|expected|at least|at most|valid|like|"
                            r"characters|digits|between|one of)\b", re.IGNORECASE)
CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]+(?:[_-][A-Z0-9]+)+\b|\bcode[:=\s]+\S+", re.IGNORECASE)


def is_generic(message: str) -> bool:
    low = message.lower()
    return any(g in low for g in GENERIC)


def has_error_code(message: str, status: int | None) -> bool:
    if status and re.search(rf"\b{status}\b", message):
        return True
    return bool(re.search(r"\b[A-Z][A-Z0-9]+(?:[_-][A-Z0-9]+)+\b", message)
                or re.search(r"\bcode[:=\s]+\S+", message, re.IGNORECASE))


def check_error_message(message: str | None, status: int | None = None, field: str | None = None) -> list[str]:
    """What is wrong with the message a user sees after a failed request; empty when it is fine."""
    if not message or not message.strip():
        return ["no error message is shown"]
    problems: list[str] = []
    low = message.lower()
    if status == 422 or field:
        if field and not any(v in low for v in {field.lower(), field.lower().replace("_", " "),
                                                field.lower().replace("_", "")}):
            problems.append(f"a 422 must say which field is wrong ('{field}'): \"{message.strip()}\"")
        if not FORMAT_HINT_RE.search(message):
            problems.append(f"a 422 must say what format is expected: \"{message.strip()}\"")
    elif is_generic(message) and not has_error_code(message, status):
        problems.append("generic message without an error code"
                        + (f" (the response was HTTP {status})" if status else "") + f": \"{message.strip()}\"")
    elif status and status >= 500 and not has_error_code(message, status):
        problems.append(f"an unexpected error must show the HTTP status ({status}): \"{message.strip()}\"")
    return problems
