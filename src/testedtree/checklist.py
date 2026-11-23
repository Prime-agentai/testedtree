"""Diff-triggered reliability checklist, read from the base ref.

``.testedtree/checklist.toml``::

    [[item]]
    id = "persisted-state-compat"
    paths = ["src/storage/**", "**/migrations/**"]   # scope (default: every file)
    added = ['\\bALTER TABLE\\b']                      # regexes on added lines
    removed = []                                       # regexes on removed lines
    check = "New fields need safe defaults when an older file is loaded."
    why = "--resume crashed on transcripts written by 2.1.200"

An item fires when a changed file is in scope and, if ``added``/``removed`` are
given, at least one added/removed line in scope matches one of them. The list
is read from a committed ref (the configured base, else HEAD), never from the
working copy, so a change can't edit the checklist it is judged by.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .gitio import Repo


class ChecklistError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    id: str
    check: str
    paths: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    why: str = ""


@dataclass(frozen=True)
class FileDiff:
    path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Hit:
    item: Item
    paths: tuple[str, ...]
    lines: tuple[str, ...]  # a few matching lines, for context


@lru_cache(maxsize=512)
def glob_to_regex(glob: str) -> re.Pattern[str]:
    """gitignore-flavoured globs: ``**`` crosses directories, a slash-less
    pattern matches the basename at any depth, a trailing ``/`` means a dir."""
    g = glob.strip()
    anchored = "/" in g.rstrip("/")
    if g.endswith("/"):
        g += "**"
    g = g.lstrip("/")
    i, out = 0, []
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = g.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = g[i + 1 : j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    rx = "".join(out)
    return re.compile(rx if anchored else f"(?:.*/)?{rx}")


def path_matches(path: str, globs: tuple[str, ...]) -> bool:
    return not globs or any(glob_to_regex(g).fullmatch(path) for g in globs)


def _strs(v: Any, where: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ChecklistError(f"{where} must be a list of strings")
    return tuple(v)


def parse(text: str, source: str = "checklist") -> list[Item]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ChecklistError(f"{source}: {exc}") from exc
    raw = data.get("item", [])
    if not isinstance(raw, list):
        raise ChecklistError(f"{source}: use [[item]] tables")
    items: list[Item] = []
    ids: set[str] = set()
    for n, d in enumerate(raw, 1):
        where = f"{source} item #{n}"
        if not isinstance(d, dict):
            raise ChecklistError(f"{where}: must be a table")
        bad = set(d) - {"id", "check", "paths", "added", "removed", "why"}
        if bad:
            raise ChecklistError(f"{where}: unknown key(s) {', '.join(sorted(bad))}")
        iid, chk = d.get("id"), d.get("check")
        if not isinstance(iid, str) or not iid:
            raise ChecklistError(f"{where}: needs id")
        if iid in ids:
            raise ChecklistError(f"{where}: duplicate id {iid!r}")
        ids.add(iid)
        if not isinstance(chk, str) or not chk.strip():
            raise ChecklistError(f"{where} ({iid}): needs check = \"what to verify\"")
        added = _strs(d.get("added"), f"{where} added")
        removed = _strs(d.get("removed"), f"{where} removed")
        for rx in (*added, *removed):
            try:
                re.compile(rx)
            except re.error as exc:
                raise ChecklistError(f"{where} ({iid}): bad regex {rx!r}: {exc}") from exc
        items.append(
            Item(iid, chk.strip(), _strs(d.get("paths"), f"{where} paths"), added, removed, str(d.get("why", "")))
        )
    return items


def parse_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff -U0 --no-renames`` output into added/removed lines per file."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    old_path: str | None = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur, old_path, in_hunk = None, None, False
            continue
        if not in_hunk and line.startswith("--- "):
            p = line[4:]
            old_path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if not in_hunk and line.startswith("+++ "):
            p = line[4:]
            new_path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            path = new_path or old_path
            if path is not None:
                cur = FileDiff(path)
                files.append(cur)
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("Binary files ") and cur is None:
            # binary: path only, from the "Binary files a/x and b/x differ" line
            m = re.match(r"Binary files (?:a/(.*)|/dev/null) and (?:b/(.*)|/dev/null) differ", line)
            if m:
                files.append(FileDiff(m.group(2) or m.group(1)))
            continue
        if cur is None or not in_hunk:
            continue
        if line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def match(items: list[Item], diff: list[FileDiff]) -> list[Hit]:
    hits: list[Hit] = []
    for it in items:
        paths: list[str] = []
        lines: list[str] = []
        add_rx = [re.compile(r) for r in it.added]
        rem_rx = [re.compile(r) for r in it.removed]
        for fd in diff:
            if not path_matches(fd.path, it.paths):
                continue
            if not add_rx and not rem_rx:
                paths.append(fd.path)
                continue
            found = [f"+ {ln.strip()}" for ln in fd.added if any(r.search(ln) for r in add_rx)]
            found += [f"- {ln.strip()}" for ln in fd.removed if any(r.search(ln) for r in rem_rx)]
            if found:
                paths.append(fd.path)
                lines.extend(f"{fd.path}: {ln[:160]}" for ln in found[:2])
        if paths:
            hits.append(Hit(it, tuple(paths), tuple(lines[:3])))
    return hits


def resolve_base(repo: Repo, configured: str | None) -> str | None:
    """The ref the checklist is read from: configured base, else HEAD."""
    for ref in ([configured] if configured else []) + ["HEAD"]:
        if ref and repo.resolve(ref):
            return ref
    return None


def load(repo: Repo, file: str, base: str | None) -> tuple[list[Item], str | None]:
    """Items from ``file`` at the base ref; ``([], None)`` when there is none."""
    ref = resolve_base(repo, base)
    if ref is None:
        return [], None
    text = repo.show_file(ref, file)
    if text is None:
        return [], ref
    return parse(text, f"{ref}:{file}"), ref


def render_md(hits: list[Hit], ref: str | None, total: int) -> str:
    if not hits:
        return f"No checklist items match this diff ({total} items at {ref or 'no ref'})."
    out = [f"**{len(hits)} of {total} checklist items match this diff** (read from `{ref}`):", ""]
    for h in hits:
        out.append(f"- [ ] **{h.item.id}**: {h.item.check}")
        out.append(f"  - triggered by: {', '.join(f'`{p}`' for p in h.paths[:5])}{' …' if len(h.paths) > 5 else ''}")
        for ln in h.lines:
            out.append(f"  - `{ln}`")
        if h.item.why:
            out.append(f"  - why: {h.item.why}")
    return "\n".join(out)


EXAMPLE = '''# Diff-triggered checklist. Each item fires only when the diff can show it matters.
# Read from a committed ref (HEAD, or [checklist] base), never the working copy.

[[item]]
id = "dependency-change"
paths = ["pyproject.toml", "package.json", "go.mod", "Cargo.toml", "*.lock", "requirements*.txt"]
check = "A dependency changed: is it pinned, licensed compatibly, and needed?"

[[item]]
id = "secrets-in-logs"
added = ['(?i)\\b(log|print|console\\.log|logger\\.\\w+)\\(.*(token|secret|password|authorization)']
check = "A log line mentions a credential: make sure the value is redacted."

[[item]]
id = "migration-reversible"
paths = ["**/migrations/**", "*.sql"]
check = "Can this migration run twice, and be rolled back, without data loss?"

[[item]]
id = "test-weakened"
paths = ["tests/**", "test/**", "**/*_test.*", "**/*.test.*", "**/test_*"]
removed = ['\\bassert', '\\bexpect\\(']
check = "Assertions were removed from a test: was the behaviour really meant to change?"
'''
