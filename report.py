"""Validation of reported violations and assembly of the final JSON report."""

import re

from redact import redact
from tools import RepoTools

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("repository_hygiene", "dependency_management", "code_quality", "testing", "ci",
              "security", "configuration", "collaboration", "build_release")
TEXT_FIELDS = ("description", "evidence", "recommendation")
RULE_ID = re.compile(r"^[A-Z]{2,5}-\d{3}$")


class InvalidViolation(ValueError):
    """Raised with a message the agent can act on."""


def validate_violation(args: dict, tools: RepoTools) -> dict:
    """Return a normalized violation or raise InvalidViolation explaining what to fix."""
    rule_id = str(args.get("rule_id", "")).strip().upper()
    if not RULE_ID.match(rule_id):
        raise InvalidViolation("rule_id must look like 'SEC-001'")
    category = str(args.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        raise InvalidViolation(f"category must be one of {CATEGORIES}")
    severity = str(args.get("severity", "")).strip().lower()
    if severity not in SEVERITIES:
        raise InvalidViolation(f"severity must be one of {SEVERITIES}")
    texts = {}
    for key in TEXT_FIELDS:
        value = str(args.get(key) or "").strip()
        if not value:
            raise InvalidViolation(f"'{key}' is required")
        texts[key] = redact(value)[:1000]

    file = str(args.get("file") or "").strip().removeprefix("./").lstrip("/") or None
    line = args.get("line")
    try:
        line = int(line) if line not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise InvalidViolation("line must be an integer or omitted") from None

    if file is not None:
        if file not in set(tools.tracked_files()):
            raise InvalidViolation(
                f"'{file}' is not a tracked file in this repository. Use an exact path from list_files; "
                "for something that is missing or only in git history, omit file and line.")
        if line is not None:
            count = tools.line_count(file) or 0
            if not 1 <= line <= count:
                raise InvalidViolation(f"line {line} is outside '{file}' (1-{count})")
            if file not in tools.inspected:
                raise InvalidViolation(f"you have not inspected '{file}' yet; read it to confirm the line first")
    elif line is not None:
        raise InvalidViolation("line given without file")

    return {"rule_id": rule_id, "category": category, "severity": severity, "file": file, "line": line, **texts}


def dedupe_key(v: dict) -> tuple:
    return (v["rule_id"], v["file"], v["line"])


def build_report(repository: str, violations: list[dict]) -> dict:
    ordered = sorted(violations, key=lambda v: (SEVERITIES.index(v["severity"]), v["rule_id"], v["file"] or ""))
    fields = ("rule_id", "category", "severity", "file", "line", "description", "evidence", "recommendation")
    return {"repository": repository, "violations": [{k: v[k] for k in fields} for v in ordered]}
