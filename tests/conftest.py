from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
# the interpreter running the tests, quoted for the shell (also on Windows)
PY = f'"{Path(sys.executable).as_posix()}"'


class Sandbox:
    """A throwaway git repo with helpers that drive git and the real CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(SRC) + os.pathsep + self.env.get("PYTHONPATH", "")
        self.env["TESTEDTREE_PYTHON"] = sys.executable
        for k in ("TESTEDTREE_SKIP", "TESTEDTREE_SESSION", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
            self.env.pop(k, None)
        self.env.update(
            GIT_AUTHOR_NAME="Dev",
            GIT_AUTHOR_EMAIL="dev@example.com",
            GIT_COMMITTER_NAME="Dev",
            GIT_COMMITTER_EMAIL="dev@example.com",
            GIT_CONFIG_NOSYSTEM="1",
            HOME=str(root.parent / "home"),
        )
        (root.parent / "home").mkdir(exist_ok=True)

    def git(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        e = dict(self.env)
        if env:
            e.update(env)
        p = subprocess.run(["git", *args], cwd=self.root, env=e, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise AssertionError(f"git {args} failed: {p.stdout}{p.stderr}")
        return p

    def tt(self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        e = dict(self.env)
        if env:
            e.update(env)
        return subprocess.run(
            [sys.executable, "-m", "testedtree", *args], cwd=self.root, env=e, capture_output=True, text=True, input=stdin
        )

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit_all(self, msg: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.git("add", "-A")
        return self.git("commit", "-m", msg, check=False, env=env)

    def last_message(self) -> str:
        return self.git("log", "-1", "--format=%B").stdout

    def config(self, mode: str = "enforce", extra: str = "", require: str = '["tests"]') -> None:
        self.write(
            ".testedtree.toml",
            f'[gate]\nmode = "{mode}"\nrequire = {require}\n{extra}\n'
            f"[checks.tests]\nrun = '{PY} check_app.py'\n"
            f"[checks.lint]\nrun = '{PY} lint_app.py'\n",
        )


CHECK_APP = 'import sys\nsys.exit(0 if "BUG" not in open("app.py").read() else 1)\n'
LINT_APP = "import sys\nsys.exit(0)\n"


@pytest.fixture
def sb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Sandbox]:
    root = tmp_path / "repo"
    root.mkdir()
    s = Sandbox(root)
    s.git("init", "-q", "-b", "main")
    s.git("config", "commit.gpgsign", "false")
    s.write("app.py", "print('hello')\n")
    s.write("check_app.py", CHECK_APP)
    s.write("lint_app.py", LINT_APP)
    s.write(".gitignore", "__pycache__/\n*.log\n")
    monkeypatch.chdir(root)
    for k in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "TESTEDTREE_SKIP", "TESTEDTREE_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Dev")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "dev@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Dev")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "dev@example.com")
    yield s


@pytest.fixture
def adopted(sb: Sandbox) -> Sandbox:
    """Config committed, hooks installed, enforce mode."""
    sb.config("enforce")
    assert sb.commit_all("adopt testedtree").returncode == 0
    r = sb.tt("install")
    assert r.returncode == 0, r.stderr
    return sb
