"""``testedtree report``: what share of history was tested as committed.

Reads ``Tested-Tree:`` trailers over a commit range. It splits agent from human
commits (by author or ``Co-authored-by``), lists commits that edited the policy
itself (config or checklist), and counts checklist hits per item so items that
never fire can be pruned ("rules only grow if you let them").
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Literal

from . import checklist
from .config import CONFIG_NAME, Config
from .gitio import Repo

Kind = Literal["verified", "override", "skipped", "none", "unstamped"]
AGENT_RE = re.compile(r"claude|codex|copilot|cursor|gemini|aider|devin|openhands|\[bot\]|noreply@anthropic", re.I)


@dataclass
class CommitRow:
    sha: str
    subject: str
    author: str
    agent: bool
    kind: Kind
    stamp: str | None
    merge: bool
    policy_change: bool = False
    checklist_hits: list[str] = field(default_factory=list)


def classify(stamp: str | None) -> Kind:
    if stamp is None:
        return "unstamped"
    s = stamp.strip().lower()
    if not s.startswith("none"):
        return "verified"
    if "override" in s:
        return "override"
    if "skipped" in s:
        return "skipped"
    return "none"


def collect(repo: Repo, cfg: Config, rev_range: str, limit: int = 500) -> list[CommitRow]:
    if not repo.head_commit():
        return []
    fmt = "%H%x1f%P%x1f%an <%ae>%x1f%s%x1f%(trailers:key=Tested-Tree,valueonly,separator=%x1d)%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1d)%x1e"
    out = repo.run(["log", f"--max-count={limit}", f"--format={fmt}", rev_range, "--"])
    items: list[checklist.Item] = []
    try:
        items, _ = checklist.load(repo, cfg.checklist_file, cfg.checklist_base)
    except checklist.ChecklistError:
        items = []
    policy = {CONFIG_NAME, cfg.checklist_file}
    rows: list[CommitRow] = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 6:
            continue
        sha, parents, author, subject, stamp_raw, coauth = parts[:6]
        stamps = [s.strip() for s in stamp_raw.split("\x1d") if s.strip()]
        stamp = stamps[-1] if stamps else None
        merge = len(parents.split()) > 1
        parent = parents.split()[0] if parents else None
        parent_tree = repo.tree_of(parent) if parent else repo.empty_tree()
        tree = repo.tree_of(sha) or repo.empty_tree()
        changed = set(repo.changed_paths(parent_tree or repo.empty_tree(), tree))
        hits: list[str] = []
        if items and not merge:
            diff = checklist.parse_diff(repo.diff_text(parent_tree or repo.empty_tree(), tree))
            hits = [h.item.id for h in checklist.match(items, diff)]
        rows.append(
            CommitRow(
                sha=sha,
                subject=subject,
                author=author,
                agent=bool(AGENT_RE.search(author) or AGENT_RE.search(coauth)),
                kind=classify(stamp),
                stamp=stamp,
                merge=merge,
                policy_change=bool(changed & policy),
                checklist_hits=hits,
            )
        )
    return rows


def summary(rows: list[CommitRow], items: list[checklist.Item]) -> dict[str, object]:
    def counts(sel: list[CommitRow]) -> dict[str, int]:
        c = Counter(r.kind for r in sel)
        return {k: c.get(k, 0) for k in ("verified", "none", "override", "skipped", "unstamped")} | {"total": len(sel)}

    hits = Counter(h for r in rows for h in r.checklist_hits)
    return {
        "all": counts(rows),
        "agent": counts([r for r in rows if r.agent]),
        "human": counts([r for r in rows if not r.agent]),
        "policy_changes": [r.sha[:12] + " " + r.subject for r in rows if r.policy_change],
        "checklist_hits": {i.id: hits.get(i.id, 0) for i in items},
        "never_fired": [i.id for i in items if hits.get(i.id, 0) == 0],
    }


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "n/a"


def render(rows: list[CommitRow], items: list[checklist.Item], fmt: str, rev_range: str) -> str:
    s = summary(rows, items)
    if fmt == "json":
        return json.dumps({"range": rev_range, "summary": s, "commits": [asdict(r) for r in rows]}, indent=2)
    md = fmt == "md"
    out: list[str] = []
    out.append(("## " if md else "") + f"testedtree report: {rev_range} ({len(rows)} commits)")
    out.append("")
    if md:
        out += ["| who | commits | tested as committed | none | override | skipped | unstamped |", "|---|---:|---:|---:|---:|---:|---:|"]
    for who in ("all", "agent", "human"):
        c = s[who]
        assert isinstance(c, dict)
        if not c["total"]:
            continue
        cells = [who, str(c["total"]), f"{c['verified']} ({_pct(c['verified'], c['total'])})", str(c["none"]), str(c["override"]), str(c["skipped"]), str(c["unstamped"])]
        out.append("| " + " | ".join(cells) + " |" if md else f"  {who:<6} {c['total']:>4} commits   tested {c['verified']:>4} ({_pct(c['verified'], c['total'])})   none {c['none']}  override {c['override']}  skipped {c['skipped']}  unstamped {c['unstamped']}")
    untested = [r for r in rows if r.kind in ("none", "override", "skipped")]
    if untested:
        out += ["", ("### " if md else "") + "Commits stamped as not tested"]
        for r in untested[:30]:
            out.append(f"- `{r.sha[:12]}` {r.subject[:70]}: {r.stamp}" if md else f"  {r.sha[:12]} {r.subject[:60]}  [{r.stamp}]")
    pol = s["policy_changes"]
    assert isinstance(pol, list)
    if pol:
        out += ["", ("### " if md else "") + "Commits that changed the policy (config or checklist): review these"]
        out += [f"- `{p}`" if md else f"  {p}" for p in pol[:30]]
    hits = s["checklist_hits"]
    assert isinstance(hits, dict)
    if hits:
        out += ["", ("### " if md else "") + "Checklist items: hits in this range"]
        for iid, n in sorted(hits.items(), key=lambda kv: -kv[1]):
            out.append(f"- `{iid}`: {n}" if md else f"  {n:>4}  {iid}")
        never = s["never_fired"]
        assert isinstance(never, list)
        if never:
            out.append(("" if md else "  ") + f"Never fired here (prune candidates): {', '.join(never)}")
    return "\n".join(out) + "\n"
