import subprocess

import pytest

from redact import redact
from tools import TOOL_SCHEMAS, RepoTools, ToolError


@pytest.fixture
def tools(sample_repo):
    return RepoTools(sample_repo)


def _status(root):
    return subprocess.run(["git", "-C", str(root), "status", "--porcelain"], check=True, capture_output=True, text=True).stdout


def test_schemas_match_methods():
    for schema in TOOL_SCHEMAS:
        assert callable(getattr(RepoTools, schema["name"]))


def test_rejects_non_git_dir(tmp_path):
    with pytest.raises(ValueError):
        RepoTools(tmp_path)


def test_list_files(tools):
    out = tools.list_files()
    assert "7 tracked files" in out
    assert ".env" in out and "app/config.py" in out
    assert "ci.yml" in tools.list_files(subdir=".github")
    assert "app/main.py" not in tools.list_files(subdir=".github")


def test_read_file_numbers_lines_and_redacts(tools):
    out = tools.read_file("app/config.py")
    assert "    3| DEBUG = True" in out
    assert "sk-abcdefghijklmnop" not in out and "[REDACTED" in out
    assert "app/config.py" in tools.inspected


def test_read_file_range(tools):
    out = tools.read_file("app/config.py", start_line=3, end_line=3)
    assert "lines 3-3 of 5" in out and "API_KEY" not in out


def test_read_file_errors(tools):
    with pytest.raises(ToolError, match="does not exist"):
        tools.read_file("nope.py")
    with pytest.raises(ToolError, match="outside the repository"):
        tools.read_file("../../etc/passwd")
    with pytest.raises(ToolError, match="directory"):
        tools.read_file("app")


def test_search_code(tools):
    out = tools.search_code(r"DEBUG\s*=\s*True")
    assert "app/config.py:3:" in out
    assert "No matches" in tools.search_code("definitely_not_here_42")
    assert "app/main.py" in tools.search_code("print", path_glob="*.py")
    with pytest.raises(ToolError):
        tools.search_code("(unclosed")


def test_search_redacts_secrets(tools):
    out = tools.search_code("SECRET_TOKEN")
    assert "abc123def456" not in out


def test_git_log(tools):
    out = tools.git_log()
    assert "Initial commit" in out and "Call main" in out
    only = tools.git_log(path="README.md")
    assert "Initial commit" in only and "Call main" not in only


def test_git_log_deleted_and_git_show(sample_repo):
    import subprocess as sp
    (sample_repo / "old.key").write_text("token = 'abc123def456ghi789'\n")
    sp.run(["git", "-C", str(sample_repo), "add", "old.key"], check=True)
    sp.run(["git", "-C", str(sample_repo), "commit", "-qm", "add key"], check=True)
    sp.run(["git", "-C", str(sample_repo), "rm", "-q", "old.key"], check=True)
    sp.run(["git", "-C", str(sample_repo), "commit", "-qm", "remove key"], check=True)
    tools = RepoTools(sample_repo)
    out = tools.git_log(deleted_only=True)
    assert "remove key" in out and "D\told.key" in out and "Call main" not in out
    commit = out.split()[1]
    shown = tools.git_show(f"{commit}^", "old.key")
    assert "token" in shown and "abc123def456" not in shown
    with pytest.raises(ToolError):
        tools.git_show("--output=/tmp/x", "old.key")
    with pytest.raises(ToolError):
        tools.git_show(commit, "old.key")  # deleted in that commit


def test_check_gitignore(tools):
    out = tools.check_gitignore([".env", "__pycache__/x.pyc"])
    assert ".env: NOT ignored; tracked" in out
    assert "__pycache__/x.pyc: ignored by" in out


def test_run_ruff(tools):
    out = tools.run_ruff()
    assert "ruff statistics" in out
    assert "F401" in out  # unused `import os` in app/main.py


def test_tools_leave_repo_unchanged(tools, sample_repo):
    before = _status(sample_repo)
    tools.list_files(); tools.read_file("README.md"); tools.search_code("x")
    tools.git_log(); tools.git_log(deleted_only=True); tools.check_gitignore([".env"]); tools.run_ruff()
    assert _status(sample_repo) == before == ""


@pytest.mark.parametrize("text,leak", [
    ('password = "hunter2hunter2"', "hunter2"),
    ("DB_PASSWORD=Sup3rS3cretValue", "Sup3r"),
    ("ghp_" + "a" * 36, "aaaaaaaa"),
    ("postgres://admin:p4ssw0rd@db/x", "p4ssw0rd"),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCD"),
])
def test_redact_hides_secrets(text, leak):
    assert leak not in redact(text)


@pytest.mark.parametrize("text", [
    'api_key = os.environ["API_KEY"]',
    "token: ${{ secrets.GH_TOKEN }}",
    "password = get_password()",
    'SECRET_KEY = "changeme"',
])
def test_redact_keeps_non_secrets(text):
    assert redact(text) == text
