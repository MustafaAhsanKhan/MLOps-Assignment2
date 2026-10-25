# DevOps Repository Auditing Agent

An AI agent that clones a public GitHub repository, investigates it with inspection tools that
**the LLM chooses step by step**, and writes a machine-readable JSON report of DevOps
best-practice violations with evidence, severity and remediation.

- LLM: Google Gemini (`gemini-3.8-flash` by default) through the official `google-genai` SDK
- No agent frameworks (no LangChain / LangGraph / n8n): the reasoning loop, tool dispatch and
  control flow are plain Python in [`agent_loop.py`](agent_loop.py)
- The inspected repository is never modified and its code is never executed

---

## 1. Requirements

- Python 3.10 or newer
- `git` on the `PATH`
- A Gemini API key ([Google AI Studio](https://aistudio.google.com/apikey))
- Network access to `github.com` and the Gemini API

## 2. Installation

```bash
git clone https://github.com/MustafaAhsanKhan/devops-repo-audit-agent.git
cd devops-repo-audit-agent
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configuration

Copy the template and put your API key in it:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes | Gemini API key (`GOOGLE_API_KEY` is accepted as a fallback) |
| `GEMINI_MODEL` | no | Model id, default `gemini-3.8-flash` |

Exporting the variables in the shell works too (`export GEMINI_API_KEY=...`); `.env` is optional.
`.env` is git-ignored, so keys are never committed.

## 4. Running the agent

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

The run is fully unattended. Progress is printed to stderr; the report goes to `--output`.

| Option | Default | Description |
|---|---|---|
| `--repo` | (required) | Public GitHub repository URL (`https://github.com/owner/repo`, `.git` suffix allowed) |
| `--output` | (required) | Path of the JSON report |
| `--model` | `$GEMINI_MODEL` or `gemini-3.8-flash` | Gemini model id |
| `--max-steps` | `40` | Maximum agent turns (each turn may contain several tool calls) |
| `--trace` | `<output>.trace.jsonl` | Tool-call trace: every model turn (reasoning summary + chosen tools) and every tool result |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Audit completed, report written (`violations` may be empty) |
| 1 | Unexpected internal error |
| 2 | Invalid input or configuration (bad URL, missing API key, bad option) |
| 3 | Repository could not be cloned (missing/private repo, network, timeout, no `git`) |
| 4 | Gemini API failure after retries (invalid key, quota, outage) |

No report file is written on a nonzero exit.

## 5. Output format

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

- `severity` is one of `critical`, `high`, `medium`, `low`.
- `category` is one of `repository_hygiene`, `dependency_management`, `code_quality`, `testing`,
  `ci`, `security`, `configuration`, `collaboration`, `build_release`.
- `file` and `line` are `null` for repository-level findings (e.g. no tests at all).
- Violations are sorted by severity. Secret values never appear (they are shown as `[REDACTED len=N]`).

See [`sample_report.json`](sample_report.json) for real output and
[`sample_trace.jsonl`](sample_trace.jsonl) for the matching tool-call trace.

---

## 6. Architecture

```
agent.py        CLI: argument parsing, URL validation, clone into a temp dir, exit codes, writes JSON
agent_loop.py   Auditor: the observe -> reason -> select tool -> execute -> interpret loop
llm.py          GeminiLLM: google-genai wrapper (function declarations, history, retries, timeouts)
tools.py        RepoTools: read-only inspection tools + their JSON schemas
prompts.py      System prompt with inspection areas, rule ids, severity and evidence rules
report.py       Validation of reported violations, de-duplication, final report assembly
redact.py       Secret redaction for tool output, traces and the report
tests/          pytest suite (tools on a throwaway git repo, agent loop driven by a scripted fake LLM)
```

### The agent loop

```
             +--------------------------------------------------------------+
             |                                                              v
 repo URL -> clone -> [ Gemini: reason over history, choose tool call(s) ] -> dispatch -> tool result
                                     ^                                            |
                                     +-------------- interpret result ------------+
                                                  ... until finish (or budget)
                     -> validated findings -> report.json  (+ trace.jsonl)
```

1. **Observe** - the model gets the task and calls `list_files` to see the repository.
2. **Reason / select tool** - every turn, Gemini decides which tool(s) to call next based on what it has seen
   (function-calling mode `ANY`, so each turn is a tool call; automatic function execution in the SDK is
   disabled). Python never picks what to inspect.
3. **Execute** - `Auditor.dispatch` runs the tool and returns the result (or an error message) to the model.
   A failing tool never crashes the run; the model sees the error and adapts.
4. **Interpret** - the model reads the results and decides on follow-ups (e.g. a CI file shows `pytest || true`,
   so it reads the workflow fully, then reports an ineffective gate).
5. **Report** - each confirmed finding is submitted with the `report_violation` tool. `report.py` validates it
   and returns feedback the model can act on:
   - the file must exist in the repo; for missing things the model must leave file/line empty
   - a line number must be within the file **and** the model must have actually read or searched that file
   - severity/category/rule id must be valid; duplicates (same rule, file, line) are refused
6. **Self-review** - the first `finish` call returns the list of accepted findings and asks the model to
   retract unsupported ones (`retract_violation`) and to check any area it skipped. The second `finish`
   ends the run. This step exists to cut false positives on healthy repositories.
7. A turn budget (`--max-steps`) bounds cost and time; 3 turns before the end the model is told to wrap up.
   If the budget runs out, the findings confirmed so far are still written.

### Tools

| Tool | Purpose |
|---|---|
| `list_files(subdir?)` | Tracked files (`git ls-files`), optionally under a directory |
| `read_file(path, start_line?, end_line?)` | File contents with line numbers (400 lines per call), binary detection |
| `search_code(pattern, path_glob?, ignore_case?)` | Python-regex search across tracked text files, `path:line:text` hits |
| `git_log(max_count?, path?, deleted_only?)` | Recent commits with changed-file stats, optionally for one path; `deleted_only` lists files deleted in each commit |
| `git_show(commit, path)` | A file's content at a past commit (to confirm secrets removed from the tree but still in history) |
| `check_gitignore(paths)` | Whether `.gitignore` rules ignore each path and whether it is tracked |
| `run_ruff(path?)` | Static lint statistics with ruff (ruff parses files, it does not run them) |
| `report_violation(...)` | Submit a confirmed finding (validated, see above) |
| `retract_violation(id, reason)` | Withdraw an accepted finding |
| `finish(summary)` | End the audit (first call triggers the self-review) |

### Inspection areas

The system prompt ([`prompts.py`](prompts.py)) gives the model the categories, rule ids and severity
scale. They guide the investigation; they are not a fixed checklist. The model decides what to look
at for each repository.

| Category | Rule ids |
|---|---|
| Repository hygiene | `HYG-001` missing README, `HYG-002` unsuitable .gitignore, `HYG-003` tracked generated/temp files |
| Dependency management | `DEP-001` no manifest, `DEP-002` weak constraints, `DEP-003` inconsistent specs, `DEP-004` missing lockfile |
| Code quality | `CQ-001` no linting/formatting, `CQ-002` clear maintainability problems |
| Testing | `TEST-001` no tests, `TEST-002` tests not run in CI, `TEST-003` broken tests |
| GitHub Actions / CI | `CI-001` wrong triggers, `CI-002` ineffective gates, `CI-003` skipped validation, `CI-004` unpinned/deprecated actions |
| Security | `SEC-001` hardcoded secrets, `SEC-002` tracked secret files / secrets in history, `SEC-003` unsafe workflow permissions, `SEC-004` unsafe workflow patterns, `SEC-005` insecure config |
| Configuration | `CFG-001` machine-specific paths, `CFG-002` committed local settings, `CFG-003` poor environment separation |
| Collaboration | `COL-001` unsafe PR automation, `COL-002` missing review guidance where relevant |
| Build and release | `BLD-001` missing build validation, `BLD-002` risky release/deploy automation |

To keep false positives low, the prompt tells the model to accept equivalent alternatives (e.g.
`pyproject.toml` instead of `requirements.txt`, any lockfile format, linters configured in
`pyproject.toml` or pre-commit, non-GitHub CI), to ignore placeholders in example files, not to infer
remote branch protection, and to report one finding per root cause.

### Safety

- The target is cloned into a temporary directory that is deleted after the run; the remote repository is
  only read.
- No tool executes repository code (no `pytest`, no installs, no build scripts). `run_ruff` parses source files
  without importing them and runs with `--no-cache`, so it writes nothing.
- Git runs with `GIT_TERMINAL_PROMPT=0` (no credential prompts), Git LFS downloads disabled, and the user's global/system
  git config ignored.
- `read_file` refuses paths outside the clone (including via `..` or symlinks); outputs are size-capped.
- Secrets are redacted before tool output reaches the model, the trace or the report (common token formats,
  private keys, URL credentials, `password/secret/token/api_key = "..."` literals).
- Timeouts on cloning (300 s), git/ruff commands (60 s) and Gemini calls (180 s); Gemini rate-limit and
  server errors are retried with exponential backoff.

---

## 7. Tests

```bash
pytest -q
ruff check .
```

The tests need no API key and no network:

- `tests/test_tools.py` builds a throwaway git repository with known defects and checks every tool, path-escape
  protection, redaction, and that the tools leave the repository unchanged.
- `tests/test_agent.py` drives the real `Auditor` loop with a scripted fake LLM to check tool dispatch, report
  validation and feedback, duplicate handling, retraction, the self-review step, budget enforcement, trace
  writing, secret-free output, URL validation and exit codes.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff and pytest on every push and pull request.

## 8. Limitations

- Only public GitHub repositories are supported (no authentication).
- The agent reasons over repository files only. It cannot see GitHub settings (branch protection, secrets,
  environments), so it does not report on them.
- Tests in the target repository are inspected statically, not executed, so failing tests are only found
  when the failure is visible in the source.
- Results depend on the model; runs are not fully deterministic.
