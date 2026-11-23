"""Git snapshots, config-from-HEAD and the coverage verdict."""

from __future__ import annotations

from pathlib import Path

from conftest import PY, Sandbox

from testedtree import config, ledger
from testedtree.gitio import Repo
from testedtree.runner import run_check
from testedtree.verdict import evaluate, stamp_value


def _repo(sb: Sandbox) -> Repo:
    return Repo.find(sb.root)


def test_worktree_snapshot_includes_untracked_excludes_ignored_and_leaves_index_alone(sb: Sandbox) -> None:
    sb.commit_all("base")
    repo = _repo(sb)
    sb.write("new.py", "x = 1\n")
    sb.write("debug.log", "ignored\n")
    before_index = (repo.git_dir / "index").read_bytes()
    tree = repo.worktree_tree()
    assert (repo.git_dir / "index").read_bytes() == before_index
    names = sb.git("ls-tree", "-r", "--name-only", tree).stdout.split()
    assert "new.py" in names and "debug.log" not in names
    assert sb.git("status", "--porcelain").stdout.strip() == "?? new.py"
    assert repo.staged_tree() == repo.head_tree()


def test_config_is_read_from_head_not_the_working_copy(sb: Sandbox) -> None:
    sb.config("enforce")
    repo = _repo(sb)
    pre = config.load(repo)
    assert pre.source == "worktree" and pre.notes  # not committed yet: says so
    sb.commit_all("adopt")
    sb.write(".testedtree.toml", sb.root.joinpath(".testedtree.toml").read_text().replace(f"{PY} check_app.py", "true"))
    cfg = config.load(repo)
    assert cfg.source == "HEAD" and cfg.checks["tests"].run == f"{PY} check_app.py"
    assert config.load(repo, from_worktree=True).checks["tests"].run == "true"


def test_config_errors_are_specific() -> None:
    import pytest

    with pytest.raises(config.ConfigError, match="unknown check"):
        config.parse('[gate]\nrequire=["x"]\n[checks.tests]\nrun="t"\n', "t")
    with pytest.raises(config.ConfigError, match="mode"):
        config.parse('[gate]\nmode="strict"\n[checks.tests]\nrun="t"\n', "t")
    with pytest.raises(config.ConfigError, match="bad regex"):
        config.parse('[checks.tests]\nrun="t"\nmatch=["("]\n', "t")
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.parse('[checks.tests]\nrun="t"\nrn="x"\n', "t")
    cfg = config.parse(config.render_default({"tests": 'go test ./... -run "X"'}), "t")
    assert cfg.require == ("tests",) and cfg.checks["tests"].run == 'go test ./... -run "X"'


def test_covered_then_stale_then_covered_again_after_revert(sb: Sandbox) -> None:
    sb.config("enforce")
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("app.py", "print('v2')\n")
    res = run_check(repo, cfg.checks["tests"], quiet=True)
    assert res.evidence.status == "pass"
    v = evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree())
    assert v.ok and stamp_value(v).startswith("tests@")

    sb.write("app.py", "print('v3')\n")  # edited after the run
    v = evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree())
    assert not v.ok and v.stale == ["tests"]
    assert v.checks[0].stale_paths == ("app.py",)
    assert stamp_value(v) == "none (stale: tests)"

    sb.write("app.py", "print('v2')\n")  # content-addressed: the same bytes are covered again
    assert evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree()).ok


def test_paths_coverage_ignores_unrelated_dirt_but_exact_does_not(sb: Sandbox) -> None:
    sb.config("enforce")
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("app.py", "print('v2')\n")
    sb.write("scratch.txt", "notes\n")
    run_check(repo, cfg.checks["tests"], quiet=True)
    sb.git("add", "app.py")  # commit only app.py
    v = evaluate(repo, cfg, repo.head_tree(), repo.staged_tree())
    assert v.ok and v.checks[0].other_diffs == 1  # scratch.txt differed at the time; reported, not fatal
    exact = config.parse(sb.root.joinpath(".testedtree.toml").read_text().replace("[gate]", '[gate]\ncoverage = "exact"'), "t")
    assert not evaluate(repo, exact, repo.head_tree(), repo.staged_tree()).ok


def test_missing_and_failure_newer_than_pass(sb: Sandbox) -> None:
    sb.config("enforce")
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("app.py", "BUG\n")
    v = evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree())
    assert v.missing == ["tests"]
    res = run_check(repo, cfg.checks["tests"], quiet=True)
    assert res.evidence.status == "fail" and res.evidence.exit == 1 and res.strikes == 1
    v = evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree())
    assert v.missing == ["tests"] and v.checks[0].last_failure is not None


def test_failing_run_never_counts_and_strikes_accumulate(sb: Sandbox) -> None:
    sb.config("enforce")
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("app.py", "BUG 1\n")
    strikes = [run_check(repo, cfg.checks["tests"], quiet=True).strikes for _ in range(3)]
    assert strikes == [1, 2, 3]
    assert ledger.consecutive_failures(repo, "tests") == 3
    sb.write("app.py", "fixed\n")
    assert run_check(repo, cfg.checks["tests"], quiet=True).strikes == 0
    assert ledger.consecutive_failures(repo, "tests") == 0


def test_mutating_check_vouches_for_the_tree_it_leaves(sb: Sandbox) -> None:
    sb.write(
        ".testedtree.toml",
        f"[gate]\nrequire=['fmt']\n[checks.fmt]\nrun = '{PY} fmt.py'\nmutates = true\n",
    )
    sb.write("fmt.py", "p='app.py'\nopen(p,'w').write(open(p).read().strip()+'\\n')\n")
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("app.py", "print('x')   \n\n\n")
    ev = run_check(repo, cfg.checks["fmt"], quiet=True).evidence
    assert ev.drifted and ev.tree == ev.tree_after
    assert evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree()).ok


def test_evidence_is_shared_across_worktrees(sb: Sandbox, tmp_path: Path) -> None:
    sb.config("enforce")
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("app.py", "print('feature')\n")
    run_check(repo, cfg.checks["tests"], quiet=True)
    sb.commit_all("feature", env={"TESTEDTREE_SKIP": "x"})
    wt = tmp_path / "wt"
    sb.git("worktree", "add", "-q", str(wt), "HEAD~1")
    (wt / "app.py").write_text("print('feature')\n")
    other = Repo.find(wt)
    assert other.state_dir == repo.state_dir
    assert evaluate(other, config.load(other), other.head_tree(), other.worktree_tree()).ok


def test_exempt_paths_need_no_checks_but_code_still_does(sb: Sandbox) -> None:
    sb.config("enforce", extra='exempt = ["*.md", "docs/**"]')
    sb.commit_all("adopt")
    repo = _repo(sb)
    cfg = config.load(repo)
    sb.write("docs/guide.md", "how to\n")
    sb.write("README.md", "hi\n")
    v = evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree())
    assert v.ok and stamp_value(v) == "pass (exempt paths only)"
    sb.write("app.py", "print('v2')\n")
    v = evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree())
    assert not v.ok and v.missing == ["tests"]
    run_check(repo, cfg.checks["tests"], quiet=True)
    sb.write("README.md", "edited after the run, but exempt\n")
    assert evaluate(repo, cfg, repo.head_tree(), repo.worktree_tree()).ok
