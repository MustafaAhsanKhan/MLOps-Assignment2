"""Read-only inspection tools that operate on a local clone of the target repository.

Every tool returns plain text (already secret-redacted) that is fed back to the LLM.
No tool writes to the clone or executes code from it.
"""

import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from redact import redact

MAX_OUTPUT_CHARS = 20_000
MAX_FILE_LINES = 400
MAX_LIST_FILES = 500
CMD_TIMEOUT = 60
MAX_SEARCH_BYTES = 1_000_000
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{4,40}([~^][0-9]*)?$")
# Ignore user/system git config so nothing outside our own clone can influence git.
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


class ToolError(Exception):
    """A tool failed in a way the agent should be told about (not fatal)."""


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


class RepoTools:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists():
            raise ValueError(f"{self.root} is not a git repository")
        # Files whose line numbers the agent has actually seen (read or search hit).
        self.inspected: set[str] = set()

    # ---- helpers -------------------------------------------------------
    def _run_git(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), "-c", "core.quotepath=off", *args],
                check=False, capture_output=True, text=True, timeout=CMD_TIMEOUT, errors="replace", env=GIT_ENV,
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"git {args[0]} timed out after {CMD_TIMEOUT}s") from e

    def _git(self, *args: str) -> str:
        proc = self._run_git(*args)
        if proc.returncode != 0:
            raise ToolError(proc.stderr.strip() or f"git {args[0]} failed")
        return proc.stdout

    def resolve(self, path: str) -> Path:
        """Resolve a repo-relative path, refusing anything outside the clone."""
        p = (self.root / path.lstrip("/")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path '{path}' is outside the repository")
        return p

    def tracked_files(self) -> list[str]:
        return [f for f in self._git("ls-files", "-z").split("\0") if f]

    def line_count(self, path: str) -> int | None:
        p = self.resolve(path)
        if not p.is_file():
            return None
        with p.open("rb") as fh:
            return sum(1 for _ in fh)

    # ---- tools exposed to the LLM -------------------------------------
    def list_files(self, subdir: str = "") -> str:
        files = self.tracked_files()
        if subdir:
            prefix = subdir.strip("/") + "/"
            files = [f for f in files if f.startswith(prefix)]
        total = len(files)
        shown = files[:MAX_LIST_FILES]
        out = f"{total} tracked files" + (f" under '{subdir}'" if subdir else "") + ":\n"
        out += "\n".join(shown)
        if total > len(shown):
            out += f"\n... [{total - len(shown)} more; call list_files(subdir=...) to narrow]"
        return out

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self.resolve(path)
        if p.is_dir():
            raise ToolError(f"'{path}' is a directory; use list_files(subdir='{path}')")
        if not p.is_file():
            raise ToolError(f"file '{path}' does not exist")
        raw = p.read_bytes()
        if b"\0" in raw[:8192]:
            return f"'{path}' is a binary file ({len(raw)} bytes); contents not shown."
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_FILE_LINES - 1)
        end = min(end, start + MAX_FILE_LINES - 1)
        rel = str(p.relative_to(self.root))
        self.inspected.add(rel)
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
        header = f"{rel} (lines {start}-{end} of {len(lines)})\n"
        if end < len(lines):
            body += f"\n... [more lines; call read_file with start_line={end + 1}]"
        return _truncate(redact(header + body))

    def search_code(self, pattern: str, path_glob: str = "", ignore_case: bool = False) -> str:
        # ponytail: Python `re` over tracked files; a pathological regex can be slow on huge repos.
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}") from e
        hits = []
        for rel in self.tracked_files():
            if path_glob and not (fnmatch.fnmatch(rel, path_glob) or rel.startswith(path_glob.rstrip("*"))):
                continue
            p = self.root / rel
            if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_SEARCH_BYTES:
                continue
            raw = p.read_bytes()
            if b"\0" in raw[:8192]:
                continue
            file_hits = 0
            for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{n}:{line.strip()[:300]}")
                    self.inspected.add(rel)
                    file_hits += 1
                    if file_hits >= 20:
                        break
            if len(hits) >= 200:
                hits.append("... [match limit reached; narrow the pattern or path_glob]")
                break
        if not hits:
            return f"No matches for /{pattern}/" + (f" in '{path_glob}'" if path_glob else "")
        return _truncate(redact("\n".join(hits)))

    def git_log(self, max_count: int = 20, path: str = "", deleted_only: bool = False) -> str:
        n = max(1, min(int(max_count), 100))
        args = ["log", f"--max-count={n}", "--date=short", "--format=commit %h  %ad  %an%n    %s"]
        # Deleted files are where removed secrets/artifacts hide: list them by name and status.
        args += ["--diff-filter=D", "--name-status"] if deleted_only else ["--stat=120"]
        if path:
            args += ["--", path]
        out = self._git(*args)
        return _truncate(redact(out)) or ("No commits deleting files." if deleted_only else "No commits found.")

    def git_show(self, commit: str, path: str) -> str:
        """Show a file as it existed at a past commit (e.g. a file that was later deleted)."""
        if not COMMIT_RE.match(commit or ""):
            raise ToolError("commit must be a hex commit hash, optionally followed by ^ or ~N")
        self.resolve(path)  # rejects paths escaping the repository
        proc = self._run_git("show", f"{commit}:{path.lstrip('/')}")
        if proc.returncode != 0:
            raise ToolError(proc.stderr.strip()[:300] or "git show failed")
        lines = proc.stdout.splitlines()[:MAX_FILE_LINES]
        body = "\n".join(f"{i:>5}| {line}" for i, line in enumerate(lines, 1))
        return _truncate(redact(f"{path} at commit {commit}:\n{body}"))

    def check_gitignore(self, paths: list[str]) -> str:
        """Report, for each path, whether .gitignore rules would ignore it and whether it is tracked."""
        tracked = set(self.tracked_files())
        out = self._run_git("check-ignore", "--no-index", "-v", "--", *paths).stdout
        rules = {}
        for line in out.splitlines():
            src, _, p = line.partition("\t")
            rules[p] = src
        rows = []
        for p in paths:
            status = f"ignored by {rules[p]}" if p in rules else "NOT ignored"
            rows.append(f"{p}: {status}; {'tracked' if p in tracked else 'not tracked'}")
        return "\n".join(rows)

    def run_ruff(self, path: str = ".") -> str:
        """Static lint with ruff (does not import or execute repository code)."""
        target = self.resolve(path)
        ruff = shutil.which("ruff")
        cmd = [ruff] if ruff else [sys.executable, "-m", "ruff"]
        try:
            proc = subprocess.run(
                [*cmd, "check", "--no-cache", "--exit-zero", "--statistics", "--quiet", str(target)],
                check=False, capture_output=True, text=True, timeout=CMD_TIMEOUT, cwd=self.root,
            )
        except FileNotFoundError as e:
            raise ToolError("ruff is not installed; lint check unavailable") from e
        except subprocess.TimeoutExpired as e:
            raise ToolError("ruff timed out") from e
        if proc.returncode not in (0, 1) and not proc.stdout:
            raise ToolError(f"ruff failed: {proc.stderr.strip()[:500]}")
        out = proc.stdout.strip()
        cfg = "repository ruff config" if _has_ruff_config(self.root) else "ruff default rules (no repo config)"
        return redact(f"ruff statistics using {cfg}:\n{out or 'no issues found'}")


def _has_ruff_config(root: Path) -> bool:
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return True
    pyproject = root / "pyproject.toml"
    return pyproject.exists() and "[tool.ruff" in pyproject.read_text(errors="replace")


# JSON-schema declarations sent to the LLM. Names must match RepoTools methods.
TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List files tracked by git (repository structure). Optionally restrict to a subdirectory.",
        "parameters": {
            "type": "object",
            "properties": {"subdir": {"type": "string", "description": "Repo-relative directory, e.g. '.github/workflows'"}},
        },
    },
    {
        "name": "read_file",
        "description": "Read a repository file with line numbers (max 400 lines per call). Secrets are redacted.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search tracked text files with a Python regex. Returns path:line:text hits (max 20 per file).",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression"},
                "path_glob": {"type": "string", "description": "Optional glob or dir prefix, e.g. '*.py' or '.github/'"},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_log",
        "description": ("Show recent commits with changed-file stats, optionally only those touching a path. "
                        "deleted_only=true lists commits that deleted files (e.g. secrets removed later)."),
        "parameters": {
            "type": "object",
            "properties": {"max_count": {"type": "integer"}, "path": {"type": "string"},
                           "deleted_only": {"type": "boolean"}},
        },
    },
    {
        "name": "git_show",
        "description": ("Show a file's content at a past commit (use the parent, e.g. 'abc123^', for a file "
                        "deleted in commit abc123). Secrets are redacted."),
        "parameters": {
            "type": "object",
            "properties": {"commit": {"type": "string"}, "path": {"type": "string"}},
            "required": ["commit", "path"],
        },
    },
    {
        "name": "check_gitignore",
        "description": "For each path, report whether .gitignore rules ignore it and whether it is tracked.",
        "parameters": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
        },
    },
    {
        "name": "run_ruff",
        "description": "Run the ruff linter statically (no code execution) and return rule statistics.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
]
