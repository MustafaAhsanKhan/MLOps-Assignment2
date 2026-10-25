"""The agent: an explicit observe -> reason -> select tool -> execute -> interpret loop.

The LLM chooses every tool call. Python only dispatches calls, validates reports and enforces
the step budget; it never decides what to inspect.
"""

import json
import sys
from collections.abc import Callable

from llm import ModelTurn, ToolCall
from prompts import INITIAL_MESSAGE, REVIEW_MESSAGE
from redact import redact
from report import CATEGORIES, SEVERITIES, InvalidViolation, dedupe_key, mentioned_files, validate_violation
from tools import TOOL_SCHEMAS, RepoTools, ToolError

AGENT_TOOL_SCHEMAS = [
    {
        "name": "report_violation",
        "description": "Record one confirmed DevOps violation. Omit file/line for repository-level findings.",
        "parameters": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "e.g. SEC-001"},
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "severity": {"type": "string", "enum": list(SEVERITIES)},
                "file": {"type": "string", "description": "Exact repo-relative path, omit if not applicable"},
                "line": {"type": "integer", "description": "1-based line of the offending content, omit if n/a"},
                "description": {"type": "string"},
                "evidence": {"type": "string", "description": "What you observed. Never include secret values."},
                "recommendation": {"type": "string"},
            },
            "required": ["rule_id", "category", "severity", "description", "evidence", "recommendation"],
        },
    },
    {
        "name": "retract_violation",
        "description": "Withdraw a previously accepted violation (by its id, e.g. V3) that is unsupported or a duplicate.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id", "reason"],
        },
    },
    {
        "name": "finish",
        "description": "End the audit once every inspection area has been investigated.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    },
]

ALL_TOOL_SCHEMAS = TOOL_SCHEMAS + AGENT_TOOL_SCHEMAS
REPO_TOOLS = {t["name"] for t in TOOL_SCHEMAS}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Auditor:
    def __init__(self, llm, tools: RepoTools, max_steps: int = 40, trace: Callable[[dict], None] | None = None):
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        self.trace = trace or (lambda event: None)
        self.violations: dict[str, dict] = {}
        self._next_id = 1
        self._nudged: set[tuple] = set()
        self.reviewed = False
        self.finished = False
        self.steps_used = 0

    def run(self, repo_url: str) -> list[dict]:
        message: str | list = INITIAL_MESSAGE.format(repo_url=repo_url, max_steps=self.max_steps)
        for step in range(1, self.max_steps + 1):
            self.steps_used = step
            turn: ModelTurn = self.llm.send(message)
            self.trace({"step": step, "type": "model", "reasoning": redact(turn.thoughts)[:4000],
                        "text": redact(turn.text)[:2000],
                        "tool_calls": [{"name": c.name, "args": _safe_args(c.args)} for c in turn.calls]})
            if not turn.calls:
                message = "Respond with a tool call: continue investigating, or call finish if done."
                continue

            results = []
            for call in turn.calls:
                result = self.dispatch(call)
                _log(f"[step {step}] {call.name}({_short_args(call.args)}) -> "
                     f"{'error' if 'error' in result else 'ok'}")
                self.trace({"step": step, "type": "tool", "name": call.name, "args": _safe_args(call.args),
                            "result": redact(json.dumps(result))[:3000]})
                results.append((call, result))
            if self.finished:
                break

            remaining = self.max_steps - step
            if remaining == 3:
                results[-1][1]["budget_note"] = ("Only 3 turns left: report any remaining confirmed findings now, "
                                         "then call finish.")
            message = results
        else:
            _log(f"Step budget ({self.max_steps}) exhausted; writing the {len(self.violations)} confirmed findings.")
        return list(self.violations.values())

    def dispatch(self, call: ToolCall) -> dict:
        try:
            if call.name in REPO_TOOLS:
                return {"output": getattr(self.tools, call.name)(**call.args)}
            if call.name == "report_violation":
                return self._report(call.args)
            if call.name == "retract_violation":
                vid = str(call.args.get("id", "")).strip()
                if self.violations.pop(vid, None) is None:
                    return {"error": f"no accepted violation with id '{vid}'"}
                return {"output": f"{vid} retracted"}
            if call.name == "finish":
                return self._finish()
            return {"error": f"unknown tool '{call.name}'"}
        except ToolError as e:
            return {"error": str(e)}
        except TypeError as e:  # wrong/missing arguments from the model
            return {"error": f"bad arguments for {call.name}: {e}"}
        except Exception as e:  # noqa: BLE001 - one tool failure must not kill the audit
            return {"error": f"{call.name} failed: {type(e).__name__}: {e}"}

    def _report(self, args: dict) -> dict:
        try:
            v = validate_violation(args, self.tools)
        except InvalidViolation as e:
            return {"error": f"rejected: {e}"}
        for vid, existing in self.violations.items():
            if dedupe_key(existing) == dedupe_key(v):
                return {"error": f"duplicate of {vid}; not recorded"}
        # Soft check: a file-less finding that names a tracked file probably should point at it.
        named = mentioned_files(v, self.tools.tracked_files()) if v["file"] is None else []
        if named and dedupe_key(v) not in self._nudged:
            self._nudged.add(dedupe_key(v))
            return {"error": f"not recorded yet: the finding mentions tracked file(s) {named[:3]}. Set file (and the "
                             "offending line) if it is located there; if it truly spans several files or is "
                             "repository-level, resubmit unchanged and it will be accepted."}
        vid = f"V{self._next_id}"
        self._next_id += 1
        self.violations[vid] = v
        return {"output": f"accepted as {vid}"}

    def _finish(self) -> dict:
        if not self.reviewed:
            self.reviewed = True
            listing = "\n".join(f"{vid}: [{v['severity']}] {v['rule_id']} {v['file'] or '-'}:{v['line'] or '-'} "
                                f"{v['description']}" for vid, v in self.violations.items()) or "(none)"
            return {"output": REVIEW_MESSAGE.format(findings=listing)}
        self.finished = True
        return {"output": "audit finished"}


def _safe_args(args: dict) -> dict:
    return json.loads(redact(json.dumps(args, default=str)))


def _short_args(args: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items() if k not in ("evidence", "recommendation"))
    return redact(text)[:120]
