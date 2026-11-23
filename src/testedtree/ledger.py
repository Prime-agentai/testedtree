"""Evidence ledger: an append-only JSONL file of check runs, keyed by git tree id.

It lives in ``<git-common-dir>/testedtree/`` (never in the working tree, never
committed). One record per finished check run:

    {"v": 1, "check": "tests", "status": "pass", "exit": 0,
     "tree": "<tree this run vouches for>", "tree_before": "<tree at start>",
     "tree_after": "<tree when it ended>",
     "command": "python -m pytest -q", "source": "run" | "claude-hook",
     "session": "<agent session id or null>", "at": "<ISO time>", "duration_ms": 812}

Threat model, stated plainly: this guards against honest mistakes (tests ran
before the last edit, tests never ran, a narrowed run, a check that failed and
was forgotten). An agent that deliberately forges ledger lines can defeat it,
as it could defeat any local check; CI re-running the checks is the backstop.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from .gitio import Repo, atomic_write

Status = Literal["pass", "fail"]
LEDGER_NAME = "evidence.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Evidence:
    check: str
    status: Status
    exit: int | None
    tree: str
    tree_after: str
    command: str
    source: str
    session: str | None
    at: str
    duration_ms: int
    tree_before: str = ""
    v: int = 1

    @property
    def drifted(self) -> bool:
        """The tree changed while the check ran (it wrote files, or someone edited)."""
        return (self.tree_before or self.tree) != self.tree_after


def ledger_path(repo: Repo) -> Path:
    return repo.state_dir / LEDGER_NAME


def append(repo: Repo, ev: Evidence) -> None:
    line = json.dumps(asdict(ev), sort_keys=True) + "\n"
    # O_APPEND writes of one short line are atomic enough for concurrent hooks.
    fd = os.open(str(ledger_path(repo)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _parse(line: str) -> Evidence | None:
    try:
        d = json.loads(line)
        if d.get("v") != 1 or d.get("status") not in ("pass", "fail"):
            return None
        return Evidence(
            check=str(d["check"]),
            status=d["status"],
            exit=d.get("exit"),
            tree=str(d["tree"]),
            tree_after=str(d.get("tree_after") or d["tree"]),
            command=str(d.get("command", "")),
            source=str(d.get("source", "")),
            session=d.get("session"),
            at=str(d.get("at", "")),
            duration_ms=int(d.get("duration_ms", 0)),
            tree_before=str(d.get("tree_before") or d["tree"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def records(repo: Repo) -> list[Evidence]:
    path = ledger_path(repo)
    if not path.exists():
        return []
    out: list[Evidence] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ev = _parse(line)
            if ev is not None:
                out.append(ev)
    return out


def newest_first(repo: Repo, check: str, status: Status | None = None) -> Iterator[Evidence]:
    for ev in reversed(records(repo)):
        if ev.check == check and (status is None or ev.status == status):
            yield ev


def consecutive_failures(repo: Repo, check: str, session: str | None = None) -> int:
    """Failures of ``check`` since its last pass (any tree, any session).

    With ``session``, only that session's failures are counted, so an agent is
    told about its own rounds, not about failures another session left behind.
    """
    n = 0
    for ev in newest_first(repo, check):
        if ev.status == "pass":
            break
        if session is None or ev.session == session:
            n += 1
    return n


# -- small JSON state files -------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def blocks_for(repo: Repo, session: str) -> int:
    return int(_read_json(repo.state_dir / "blocks.json").get(session, 0))


def add_block(repo: Repo, session: str) -> int:
    path = repo.state_dir / "blocks.json"
    data = _read_json(path)
    data[session] = int(data.get(session, 0)) + 1
    # keep the file small: drop the oldest sessions beyond 200
    if len(data) > 200:
        for k in list(data)[: len(data) - 200]:
            del data[k]
    atomic_write(path, json.dumps(data, indent=1, sort_keys=True))
    return int(data[session])


def write_session_hint(repo: Repo, session: str, agent: str) -> None:
    """Written by an agent hook just before it runs ``git commit``.

    The git pre-commit hook has no idea which agent session invoked it; this
    hint (valid for two minutes) lets block counts and escapes be per session.
    """
    atomic_write(
        repo.state_dir / "session-hint.json",
        json.dumps({"session": session, "agent": agent, "t": time.time()}),
    )


def read_session_hint(repo: Repo, max_age_s: float = 120.0) -> tuple[str, str] | None:
    d = _read_json(repo.state_dir / "session-hint.json")
    try:
        if time.time() - float(d["t"]) <= max_age_s:
            return str(d["session"]), str(d.get("agent", "agent"))
    except (KeyError, TypeError, ValueError):
        pass
    return None


def write_decision(repo: Repo, tree: str, decision: dict[str, Any]) -> None:
    """The pre-commit verdict, for the prepare-commit-msg stamp to reuse."""
    atomic_write(repo.state_dir / "last-gate.json", json.dumps({"tree": tree, "t": time.time(), **decision}))


def read_decision(repo: Repo, tree: str, max_age_s: float = 600.0) -> dict[str, Any] | None:
    d = _read_json(repo.state_dir / "last-gate.json")
    try:
        if d.get("tree") == tree and time.time() - float(d["t"]) <= max_age_s:
            return d
    except (TypeError, ValueError, KeyError):
        pass
    return None


def save_pending(repo: Repo, key: str, data: dict[str, Any]) -> None:
    d = repo.state_dir / "pending"
    d.mkdir(exist_ok=True)
    safe = "".join(c for c in key if c.isalnum() or c in "-_")[:100] or "anon"
    atomic_write(d / f"{safe}.json", json.dumps({**data, "t": time.time()}))
    # opportunistic cleanup of runs that never reported back
    cutoff = time.time() - 6 * 3600
    for p in d.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def pop_pending(repo: Repo, key: str) -> dict[str, Any] | None:
    safe = "".join(c for c in key if c.isalnum() or c in "-_")[:100] or "anon"
    p = repo.state_dir / "pending" / f"{safe}.json"
    data = _read_json(p)
    try:
        p.unlink()
    except FileNotFoundError:
        return None
    return data or None
