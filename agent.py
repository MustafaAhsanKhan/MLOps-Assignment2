"""CLI entry point: python agent.py --repo <github_repository_url> --output report.json"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from agent_loop import ALL_TOOL_SCHEMAS, Auditor
from llm import GeminiLLM, LLMError
from prompts import SYSTEM_PROMPT
from redact import redact
from report import build_report
from tools import RepoTools

DEFAULT_MODEL = "gemini-3.8-flash"
CLONE_TIMEOUT = 300
GITHUB_URL = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CLONE, EXIT_LLM = 0, 1, 2, 3, 4


class FatalError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def normalize_repo_url(url: str) -> str:
    m = GITHUB_URL.match(url.strip())
    if not m:
        raise FatalError(f"'{url}' is not a GitHub repository URL (expected https://github.com/<owner>/<repo>)",
                         EXIT_USAGE)
    return f"https://github.com/{m.group(1)}/{m.group(2)}"


def clone_repo(url: str, dest: Path) -> None:
    if not shutil.which("git"):
        raise FatalError("git is not installed or not on PATH", EXIT_CLONE)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    try:
        proc = subprocess.run(["git", "clone", "--quiet", "--single-branch", "--no-tags", url + ".git", str(dest)],
                              check=False, capture_output=True, text=True, timeout=CLONE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise FatalError(f"cloning {url} timed out after {CLONE_TIMEOUT}s", EXIT_CLONE) from None
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown error"
        raise FatalError(f"could not clone {url} (is it public and does it exist?): {detail}", EXIT_CLONE)


def audit(repo_path: Path, repo_url: str, llm, max_steps: int, trace_path: Path | None) -> dict:
    tools = RepoTools(repo_path)
    trace_file = trace_path.open("w") if trace_path else None
    try:
        def trace(event: dict) -> None:
            if trace_file:
                trace_file.write(json.dumps(event) + "\n")
                trace_file.flush()
        violations = Auditor(llm, tools, max_steps=max_steps, trace=trace).run(repo_url)
    finally:
        if trace_file:
            trace_file.close()
    return build_report(repo_url, violations)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def parse_args(argv):
    p = argparse.ArgumentParser(description="AI agent that audits a GitHub repository for DevOps violations.")
    p.add_argument("--repo", required=True, help="Public GitHub repository URL")
    p.add_argument("--output", required=True, type=Path, help="Path of the JSON report to write")
    p.add_argument("--model", default=None, help=f"Gemini model (default: $GEMINI_MODEL or {DEFAULT_MODEL})")
    p.add_argument("--max-steps", type=int, default=40, help="Maximum agent turns (default: 40)")
    p.add_argument("--trace", type=Path, default=None,
                   help="Tool-call trace (JSONL). Default: <output>.trace.jsonl next to the report")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    load_dotenv()
    try:
        repo_url = normalize_repo_url(args.repo)
        if args.max_steps < 5:
            raise FatalError("--max-steps must be at least 5", EXIT_USAGE)
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise FatalError("GEMINI_API_KEY is not set (copy .env.example to .env and add your key)", EXIT_USAGE)
        model = args.model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        trace_path = args.trace or args.output.with_suffix(".trace.jsonl")
        llm = GeminiLLM(api_key, model, SYSTEM_PROMPT, ALL_TOOL_SCHEMAS)

        with tempfile.TemporaryDirectory(prefix="repo-audit-") as tmp:
            repo_path = Path(tmp) / "repo"
            print(f"Cloning {repo_url} ...", file=sys.stderr)
            clone_repo(repo_url, repo_path)
            print(f"Auditing with {model} (max {args.max_steps} turns) ...", file=sys.stderr)
            report = audit(repo_path, repo_url, llm, args.max_steps, trace_path)

        write_json(args.output, report)
        print(f"Wrote {len(report['violations'])} violations to {args.output} (trace: {trace_path})",
              file=sys.stderr)
        return EXIT_OK
    except FatalError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_LLM
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - last-resort handler, still exits nonzero
        print(f"error: unexpected failure: {type(e).__name__}: {redact(str(e))}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
