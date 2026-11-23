"""Checklist globs, diff parsing, base-ref reading; report; CLI surfaces."""

from __future__ import annotations

import json

import pytest
from conftest import Sandbox

from testedtree import checklist, report
from testedtree.gitio import Repo


@pytest.mark.parametrize(
    ("glob", "path", "hit"),
    [
        ("*.sql", "db/migrations/001.sql", True),
        ("*.sql", "schema.sql", True),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/**", "src/sub/a.py", True),
        ("**/migrations/**", "app/migrations/0001.py", True),
        ("**/migrations/**", "migrations/0001.py", True),
        ("tests/", "tests/test_a.py", True),
        ("test_?.py", "pkg/test_a.py", True),
        ("[ab].txt", "a.txt", True),
        ("[!ab].txt", "a.txt", False),
        ("package.json", "web/package.json", True),
        ("/package.json", "web/package.json", False),
    ],
)
def test_globs(glob: str, path: str, hit: bool) -> None:
    assert checklist.path_matches(path, (glob,)) is hit


DIFF = """diff --git a/src/db.py b/src/db.py
index 1..2 100644
--- a/src/db.py
+++ b/src/db.py
@@ -3,0 +4 @@
+    logger.info("token=%s", token)
@@ -10 +11 @@
-    assert x
+    pass
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/logo.png b/logo.png
new file mode 100644
Binary files /dev/null and b/logo.png differ
"""


def test_parse_diff() -> None:
    files = {f.path: f for f in checklist.parse_diff(DIFF)}
    assert set(files) == {"src/db.py", "old.txt", "logo.png"}
    assert files["src/db.py"].added == ['    logger.info("token=%s", token)', "    pass"]
    assert files["src/db.py"].removed == ["    assert x"]
    assert files["old.txt"].removed == ["bye"]


def test_example_items_fire_on_the_right_lines() -> None:
    items = checklist.parse(checklist.EXAMPLE)
    hits = {h.item.id: h for h in checklist.match(items, checklist.parse_diff(DIFF))}
    assert "secrets-in-logs" in hits and hits["secrets-in-logs"].paths == ("src/db.py",)
    assert "dependency-change" not in hits
    diff2 = checklist.parse_diff(DIFF.replace("src/db.py", "tests/test_db.py"))
    assert "test-weakened" in {h.item.id for h in checklist.match(items, diff2)}


def test_checklist_parse_errors() -> None:
    with pytest.raises(checklist.ChecklistError, match="duplicate id"):
        checklist.parse('[[item]]\nid="a"\ncheck="x"\n[[item]]\nid="a"\ncheck="y"\n')
    with pytest.raises(checklist.ChecklistError, match="needs check"):
        checklist.parse('[[item]]\nid="a"\n')
    with pytest.raises(checklist.ChecklistError, match="bad regex"):
        checklist.parse('[[item]]\nid="a"\ncheck="x"\nadded=["("]\n')
    with pytest.raises(checklist.ChecklistError, match="unknown key"):
        checklist.parse('[[item]]\nid="a"\ncheck="x"\npath=["x"]\n')


def test_checklist_is_read_from_the_ref_not_the_working_copy(sb: Sandbox) -> None:
    sb.config("observe")
    sb.write(".testedtree/checklist.toml", '[[item]]\nid = "committed"\npaths = ["app.py"]\ncheck = "c"\n')
    sb.commit_all("adopt")
    sb.write(".testedtree/checklist.toml", '[[item]]\nid = "local-edit"\npaths = ["app.py"]\ncheck = "c"\n')
    sb.write("app.py", "print('v2')\n")
    out = json.loads(sb.tt("checklist", "--format", "json").stdout)
    assert out["ref"] == "HEAD" and [h["id"] for h in out["hits"]] == ["committed"]


def test_checklist_pr_range_uses_merge_base(sb: Sandbox) -> None:
    sb.config("observe")
    sb.write(".testedtree/checklist.toml", '[[item]]\nid = "app"\npaths = ["app.py"]\ncheck = "c"\n[[item]]\nid = "docs"\npaths = ["*.md"]\ncheck = "d"\n')
    sb.commit_all("adopt")
    sb.git("checkout", "-q", "-b", "feature")
    sb.write("notes.md", "x\n")
    sb.commit_all("docs")
    sb.git("checkout", "-q", "main")
    sb.write("app.py", "print('main moved')\n")
    sb.commit_all("main change")
    sb.git("checkout", "-q", "feature")
    out = json.loads(sb.tt("checklist", "--range", "main...feature", "--base", "main", "--format", "json").stdout)
    assert [h["id"] for h in out["hits"]] == ["docs"]  # main's own app.py change is not the PR's


def test_report_classifies_and_splits_agents(adopted: Sandbox) -> None:
    sb = adopted
    sb.write(".testedtree/checklist.toml", '[[item]]\nid = "app"\npaths = ["app.py"]\ncheck = "c"\n[[item]]\nid = "never"\npaths = ["nope/**"]\ncheck = "n"\n')
    sb.commit_all("checklist", env={"TESTEDTREE_SKIP": "policy"})
    sb.write("app.py", "print('v2')\n")
    sb.tt("run", "tests")
    sb.git("add", "-A")
    sb.git("commit", "-m", "agent work\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    sb.write("app.py", "print('v3')\n")
    sb.commit_all("human hotfix", env={"TESTEDTREE_SKIP": "prod down"})

    repo = Repo.find(sb.root)
    from testedtree.config import load

    cfg = load(repo)
    rows = report.collect(repo, cfg, "HEAD")
    kinds = {r.subject: (r.kind, r.agent) for r in rows}
    assert kinds["agent work"] == ("verified", True)
    assert kinds["human hotfix"] == ("skipped", False)
    assert kinds["adopt testedtree"] == ("unstamped", False)
    s = report.summary(rows, checklist.parse((sb.root / ".testedtree/checklist.toml").read_text()))
    assert s["never_fired"] == ["never"]
    assert any("checklist" in p for p in s["policy_changes"])  # type: ignore[union-attr]

    r = sb.tt("report", "--format", "md", "--range", "HEAD~2..HEAD", "--fail-on", "none")
    assert r.returncode == 1 and "| agent | 1 | 1 (100%)" in r.stdout and "prod down" in r.stdout
    assert sb.tt("report", "--range", "HEAD~2..HEAD~1", "--fail-on", "none").returncode == 0
    data = json.loads(sb.tt("report", "--format", "json").stdout)
    assert data["summary"]["all"]["total"] == 4


def test_status_and_gate_cli(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "print('v2')\n")
    r = sb.tt("status", "--strict")
    assert r.returncode == 1 and "next: testedtree run tests" in r.stdout
    assert sb.tt("run", "tests", "lint").returncode == 0
    st = json.loads(sb.tt("status", "--json").stdout)
    assert st["ok"] and {c["check"]: c["state"] for c in st["checks"]} == {"tests": "covered", "lint": "covered"}
    assert sb.tt("gate").returncode == 0  # nothing staged: vacuously fine
    sb.git("add", "-A")
    assert sb.tt("gate").returncode == 0
    sb.write("app.py", "print('v3, untested')\n")
    sb.git("add", "-A")
    r = sb.tt("gate")
    assert r.returncode == 1 and "STALE" in r.stderr


def test_run_cli_exit_codes_and_unknown_check(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "BUG\n")
    r = sb.tt("run", "tests", "lint")
    assert r.returncode == 1 and "FAIL (exit 1)" in r.stderr and "lint" not in r.stderr.split("FAIL")[1]
    r = sb.tt("run", "tests", "lint", "--keep-going")
    assert "lint: PASS" in r.stderr
    assert sb.tt("run", "nope").returncode == 2
    assert "pass" in sb.tt("ledger").stdout


def test_init_detects_pytest_and_refuses_overwrite(sb: Sandbox) -> None:
    sb.write("pyproject.toml", "[project]\nname='x'\n")
    (sb.root / "tests").mkdir()
    r = sb.tt("init")
    assert r.returncode == 0 and "tests" in r.stdout
    assert 'run = "python -m pytest -q"' in (sb.root / ".testedtree.toml").read_text()
    assert (sb.root / ".testedtree/checklist.toml").exists()
    assert "already exists" in sb.tt("init").stderr
    checklist.parse((sb.root / ".testedtree/checklist.toml").read_text())


def test_outside_a_repo_is_a_clean_error(tmp_path: object) -> None:
    import os
    import subprocess
    import sys

    from conftest import SRC

    r = subprocess.run([sys.executable, "-m", "testedtree", "status"], cwd=str(tmp_path), capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SRC), "GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert r.returncode == 2 and "not inside a git working tree" in r.stderr
