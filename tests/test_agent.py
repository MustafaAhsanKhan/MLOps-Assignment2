import json

import pytest

import agent
from agent_loop import ALL_TOOL_SCHEMAS, Auditor
from llm import ModelTurn, ToolCall
from report import build_report
from tools import RepoTools


class FakeLLM:
    """Scripted stand-in for GeminiLLM: returns pre-planned turns and records what it was sent."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.received = []

    def send(self, message):
        self.received.append(message)
        if not self.turns:
            return ModelTurn(calls=[ToolCall("finish", {"summary": "done"})])
        return ModelTurn(calls=[ToolCall(name, args, id=f"c{i}") for i, (name, args) in enumerate(self.turns.pop(0))])


SECRET_FINDING = {
    "rule_id": "SEC-001", "category": "security", "severity": "critical", "file": "app/config.py", "line": 4,
    "description": "Hardcoded API key", "evidence": "API_KEY assigned a literal in source",
    "recommendation": "Load it from an environment variable",
}


def results_of(llm, turn_index):
    """Tool results the agent sent back to the model at the start of the given turn."""
    return {call.name: result for call, result in llm.received[turn_index]}


def test_agent_dispatches_model_selected_tools(sample_repo):
    llm = FakeLLM([
        [("list_files", {})],
        [("read_file", {"path": "app/config.py"})],
        [("report_violation", SECRET_FINDING)],
        [("finish", {"summary": "x"})],
        [("finish", {"summary": "x"})],
    ])
    violations = Auditor(llm, RepoTools(sample_repo)).run("https://github.com/o/r")
    assert "app/config.py" in results_of(llm, 1)["list_files"]["output"]
    assert "DEBUG = True" in results_of(llm, 2)["read_file"]["output"]
    assert results_of(llm, 3)["report_violation"] == {"output": "accepted as V1"}
    assert "review your accepted findings" in results_of(llm, 4)["finish"]["output"]
    assert len(violations) == 1 and violations[0]["line"] == 4


def test_line_must_be_inspected_before_reporting(sample_repo):
    llm = FakeLLM([[("report_violation", SECRET_FINDING)], [("finish", {"summary": "x"})] * 2])
    violations = Auditor(llm, RepoTools(sample_repo)).run("u")
    assert "not inspected" in results_of(llm, 1)["report_violation"]["error"]
    assert violations == []


@pytest.mark.parametrize("override,msg", [
    ({"file": "does/not/exist.py"}, "not a tracked file"),
    ({"line": 999}, "outside"),
    ({"severity": "urgent"}, "severity"),
    ({"category": "misc"}, "category"),
    ({"rule_id": "bad"}, "rule_id"),
    ({"evidence": ""}, "evidence"),
])
def test_invalid_reports_are_rejected_with_feedback(sample_repo, override, msg):
    llm = FakeLLM([[("read_file", {"path": "app/config.py"})], [("report_violation", {**SECRET_FINDING, **override})]])
    violations = Auditor(llm, RepoTools(sample_repo)).run("u")
    assert msg in results_of(llm, 2)["report_violation"]["error"]
    assert violations == []


def test_repo_level_finding_without_file(sample_repo):
    finding = {**SECRET_FINDING, "rule_id": "TEST-001", "category": "testing", "severity": "medium",
               "file": None, "line": None, "description": "No tests"}
    violations = Auditor(FakeLLM([[("report_violation", finding)]]), RepoTools(sample_repo)).run("u")
    assert violations[0]["file"] is None and violations[0]["line"] is None


def test_duplicates_and_retraction(sample_repo):
    llm = FakeLLM([
        [("read_file", {"path": "app/config.py"})],
        [("report_violation", SECRET_FINDING), ("report_violation", SECRET_FINDING)],
        [("retract_violation", {"id": "V1", "reason": "test"})],
    ])
    violations = Auditor(llm, RepoTools(sample_repo)).run("u")
    second = llm.received[2][1][1]
    assert "duplicate of V1" in second["error"]
    assert violations == []


def test_tool_errors_are_returned_not_raised(sample_repo):
    llm = FakeLLM([[("read_file", {"path": "missing.txt"}), ("read_file", {}), ("no_such_tool", {})]])
    Auditor(llm, RepoTools(sample_repo)).run("u")
    errors = [r["error"] for _, r in llm.received[1]]
    assert "does not exist" in errors[0] and "bad arguments" in errors[1] and "unknown tool" in errors[2]


def test_step_budget_is_enforced(sample_repo):
    llm = FakeLLM([[("list_files", {})]] * 50)
    auditor = Auditor(llm, RepoTools(sample_repo), max_steps=5)
    auditor.run("u")
    assert len(llm.received) == 5 and not auditor.finished


def test_secrets_never_reach_report(sample_repo):
    leaky = {**SECRET_FINDING, "evidence": 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"'}
    llm = FakeLLM([[("read_file", {"path": "app/config.py"})], [("report_violation", leaky)]])
    report = build_report("u", Auditor(llm, RepoTools(sample_repo)).run("u"))
    assert "sk-abcdefghijklmnop" not in json.dumps(report)


def test_report_shape_and_order():
    low = {**SECRET_FINDING, "severity": "low", "rule_id": "HYG-001", "file": None, "line": None}
    report = build_report("https://github.com/o/r", [low, SECRET_FINDING])
    assert [v["severity"] for v in report["violations"]] == ["critical", "low"]
    assert set(report["violations"][0]) == {"rule_id", "category", "severity", "file", "line", "description",
                                            "evidence", "recommendation"}


def test_schemas_are_unique():
    names = [s["name"] for s in ALL_TOOL_SCHEMAS]
    assert len(names) == len(set(names))


def test_audit_writes_trace(sample_repo, tmp_path):
    trace = tmp_path / "t.jsonl"
    llm = FakeLLM([[("list_files", {})], [("finish", {"summary": "x"})], [("finish", {"summary": "x"})]])
    report = agent.audit(sample_repo, "https://github.com/o/r", llm, 10, trace)
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert report == {"repository": "https://github.com/o/r", "violations": []}
    assert [e["name"] for e in events if e["type"] == "tool"] == ["list_files", "finish", "finish"]


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/psf/requests", "https://github.com/psf/requests"),
    ("https://github.com/psf/requests.git", "https://github.com/psf/requests"),
    ("http://www.github.com/psf/requests/", "https://github.com/psf/requests"),
])
def test_normalize_repo_url(url, expected):
    assert agent.normalize_repo_url(url) == expected


@pytest.mark.parametrize("url", ["not a url", "https://gitlab.com/a/b", "https://github.com/onlyowner",
                                 "https://github.com/a/b/tree/main", "file:///etc/passwd"])
def test_invalid_urls_exit_nonzero(url, tmp_path, capsys):
    code = agent.main(["--repo", url, "--output", str(tmp_path / "r.json")])
    assert code == agent.EXIT_USAGE
    assert "not a GitHub repository URL" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_missing_api_key_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(agent, "load_dotenv", lambda: None)
    code = agent.main(["--repo", "https://github.com/o/r", "--output", str(tmp_path / "r.json")])
    assert code == agent.EXIT_USAGE and "GEMINI_API_KEY" in capsys.readouterr().err


def test_fileless_finding_naming_a_tracked_file_is_nudged_once(sample_repo):
    finding = {**SECRET_FINDING, "file": None, "line": None, "evidence": "app/config.py assigns a literal key"}
    llm = FakeLLM([[("report_violation", finding)], [("report_violation", finding)]])
    violations = Auditor(llm, RepoTools(sample_repo)).run("u")
    assert "app/config.py" in results_of(llm, 1)["report_violation"]["error"]
    assert results_of(llm, 2)["report_violation"] == {"output": "accepted as V1"}
    assert len(violations) == 1
