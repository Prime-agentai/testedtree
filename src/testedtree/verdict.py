"""Does the evidence cover this commit? The one function everything else calls.

A required check *covers* a commit (parent tree P -> new tree N) when some
passing run of it was recorded against a tree T such that:

* coverage = "paths" (default): every path that differs between P and N has the
  identical blob (or is identically absent) in T. The files you are committing
  are byte-for-byte the files that were tested.
* coverage = "exact": T == N.

Anything else is ``stale`` (a pass exists, but some committed files changed
since; we name them) or ``missing`` (no pass at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from . import ledger
from .checklist import path_matches
from .config import Config
from .gitio import Repo

State = Literal["covered", "stale", "missing"]
MAX_CANDIDATES = 200


@dataclass(frozen=True)
class CheckVerdict:
    check: str
    state: State
    evidence: ledger.Evidence | None = None
    stale_paths: tuple[str, ...] = ()
    other_diffs: int = 0  # paths outside the commit that differed when the check ran
    last_failure: ledger.Evidence | None = None

    @property
    def ok(self) -> bool:
        return self.state == "covered"


@dataclass(frozen=True)
class Verdict:
    parent_tree: str
    tree: str
    changed: tuple[str, ...]
    checks: tuple[CheckVerdict, ...]
    required: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)
    exempt_changed: int = 0  # changed paths that matched [gate] exempt

    @property
    def ok(self) -> bool:
        req = set(self.required)
        return all(c.ok for c in self.checks if c.check in req)

    @property
    def missing(self) -> list[str]:
        req = set(self.required)
        return [c.check for c in self.checks if c.check in req and c.state == "missing"]

    @property
    def stale(self) -> list[str]:
        req = set(self.required)
        return [c.check for c in self.checks if c.check in req and c.state == "stale"]

    def covered(self) -> list[CheckVerdict]:
        return [c for c in self.checks if c.ok]


def evaluate_check(
    repo: Repo,
    cfg: Config,
    check: str,
    parent_tree: str,
    tree: str,
    changed: set[str],
    passes: list[ledger.Evidence],
    last_failure: ledger.Evidence | None,
) -> CheckVerdict:
    if not changed:
        return CheckVerdict(check, "covered", last_failure=last_failure)
    best: tuple[int, ledger.Evidence, list[str]] | None = None
    seen: set[str] = set()
    for ev in passes[:MAX_CANDIDATES]:
        if ev.tree in seen:
            continue
        seen.add(ev.tree)
        if cfg.coverage == "exact":
            if ev.tree == tree:
                return CheckVerdict(check, "covered", ev, last_failure=last_failure)
            bad = repo.paths_differing(ev.tree, tree, changed) or sorted(changed)[:1]
        else:
            bad = repo.paths_differing(ev.tree, tree, changed)
            if not bad:
                other = len(repo.changed_paths(ev.tree, tree))
                return CheckVerdict(check, "covered", ev, other_diffs=other, last_failure=last_failure)
        if best is None or len(bad) < best[0]:
            best = (len(bad), ev, bad)
    if best is None:
        return CheckVerdict(check, "missing", last_failure=last_failure)
    return CheckVerdict(check, "stale", best[1], tuple(best[2]), last_failure=last_failure)


def evaluate(repo: Repo, cfg: Config, parent_tree: str, tree: str) -> Verdict:
    all_changed = set(repo.changed_paths(parent_tree, tree))
    changed = {p for p in all_changed if not (cfg.exempt and path_matches(p, cfg.exempt))}
    by_check: dict[str, list[ledger.Evidence]] = {n: [] for n in cfg.checks}
    last_fail: dict[str, ledger.Evidence] = {}
    for ev in reversed(ledger.records(repo)):
        if ev.check not in by_check:
            continue
        if ev.status == "pass":
            by_check[ev.check].append(ev)
        elif ev.check not in last_fail and not by_check[ev.check]:
            last_fail[ev.check] = ev  # a failure newer than any pass
    verdicts = tuple(
        evaluate_check(repo, cfg, name, parent_tree, tree, changed, by_check[name], last_fail.get(name))
        for name in cfg.checks
    )
    return Verdict(parent_tree, tree, tuple(sorted(all_changed)), verdicts, cfg.require, cfg.notes, len(all_changed) - len(changed))


def stamp_value(v: Verdict, *, override: str | None = None) -> str:
    """The ``Tested-Tree:`` trailer value for a commit with this verdict."""
    if v.ok and override is None:
        parts = [f"{c.check}@{(c.evidence.tree if c.evidence else v.tree)[:12]}" for c in v.covered()]
        if not v.changed:
            return "pass (no file changes)"
        if v.exempt_changed == len(v.changed):
            return "pass (exempt paths only)"
        return " ".join(parts) if parts else "pass (no checks configured)"
    why: list[str] = []
    if override:
        why.append(override)
    if v.missing:
        why.append("missing: " + ",".join(v.missing))
    if v.stale:
        why.append("stale: " + ",".join(v.stale))
    return "none (" + "; ".join(why) + ")" if why else "none"
