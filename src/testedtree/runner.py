"""``testedtree run``: run configured checks and record tree-bound evidence.

The tree is snapshotted before and after each run. Evidence is bound to the
tree the check *started* on, except for checks marked ``mutates = true``
(formatters, codegen), where the tree it *left behind* is the one vouched for.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

from . import ledger
from .config import Check, Config
from .gitio import Repo


@dataclass(frozen=True)
class RunResult:
    evidence: ledger.Evidence
    strikes: int  # consecutive failures including this one (0 on pass)


def record(
    repo: Repo,
    check: Check,
    *,
    ok: bool,
    exit_code: int | None,
    tree_before: str,
    tree_after: str,
    command: str,
    source: str,
    session: str | None,
    duration_ms: int,
) -> RunResult:
    bound = tree_after if check.mutates else tree_before
    ev = ledger.Evidence(
        check=check.name,
        status="pass" if ok else "fail",
        exit=exit_code,
        tree=bound,
        tree_after=tree_after,
        command=command,
        source=source,
        session=session,
        at=ledger.now_iso(),
        duration_ms=duration_ms,
        tree_before=tree_before,
    )
    ledger.append(repo, ev)
    strikes = 0 if ok else ledger.consecutive_failures(repo, check.name, session)
    return RunResult(ev, strikes)


def run_check(repo: Repo, check: Check, *, session: str | None = None, quiet: bool = False) -> RunResult:
    before = repo.worktree_tree()
    if not quiet:
        print(f"testedtree: {check.name}: $ {check.run}   [tree {before[:12]}]", file=sys.stderr, flush=True)
    t0 = time.monotonic()
    env = dict(os.environ)
    env["TESTEDTREE_CHECK"] = check.name
    try:
        proc = subprocess.run(
            check.run,
            shell=True,
            cwd=str(repo.root),
            env=env,
            timeout=check.timeout_s,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        code = None
        print(f"testedtree: {check.name}: timed out after {check.timeout_s}s", file=sys.stderr)
    duration = int((time.monotonic() - t0) * 1000)
    after = repo.worktree_tree()
    return record(
        repo,
        check,
        ok=code == 0,
        exit_code=code,
        tree_before=before,
        tree_after=after,
        command=check.run,
        source="run",
        session=session,
        duration_ms=duration,
    )


def strike_message(check: str, strikes: int, limit: int) -> str | None:
    if limit <= 0 or strikes < limit:
        return None
    return (
        f"testedtree: `{check}` has now failed {strikes} times in a row in this session. Stop changing code to make it "
        f"pass. Summarise what you tried and what you think is wrong, and ask a human how to proceed. "
        f"Do not skip, weaken or delete the check."
    )


def describe(res: RunResult, cfg: Config) -> str:
    ev = res.evidence
    if ev.status == "pass":
        msg = f"testedtree: {ev.check}: PASS on tree {ev.tree[:12]} ({ev.duration_ms} ms)"
        if ev.drifted:
            how = "vouching for the tree it produced" if ev.tree == ev.tree_after else "files it changed are not covered"
            msg += f"; the tree changed during the run ({how})"
        return msg
    code = "timeout" if ev.exit is None else f"exit {ev.exit}"
    msg = f"testedtree: {ev.check}: FAIL ({code}) on tree {ev.tree[:12]}"
    strike = strike_message(ev.check, res.strikes, cfg.strikes)
    return msg + ("\n" + strike if strike else "")
