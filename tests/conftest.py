import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FILES = {
    "README.md": "# Demo\n",
    ".gitignore": "__pycache__/\n",
    "requirements.txt": "flask\nrequests>=2.0\n",
    "app/config.py": 'import os\n\nDEBUG = True\nAPI_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\nDB = os.environ["DB"]\n',
    "app/main.py": "import os\n\n\ndef main():\n    print('hi')\n",
    ".env": "SECRET_TOKEN=abc123def456ghi789\n",
    ".github/workflows/ci.yml": "on: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest || true\n",
}


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def sample_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Initial commit")
    (root / "app/main.py").write_text(FILES["app/main.py"] + "\nmain()\n")
    _git(root, "commit", "-q", "-am", "Call main")
    return root
