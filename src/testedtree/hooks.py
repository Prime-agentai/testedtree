"""Git hooks (the authoritative gate + stamp) and the Claude Code hook adapter.

* ``pre-commit``  -> :func:`pre_commit`: evaluate the exact staged tree, then
  pass / warn (observe) / block (enforce, with a per-session escape).
* ``commit-msg``  -> :func:`commit_msg`: append ``Tested-Tree: ...`` to every
  commit, including ``none (...)``, so unverified commits are queryable.
* ``testedtree hook claude`` -> :func:`claude_hook`: capture agent test runs as
  evidence (PreToolUse snapshot + PostToolUse/PostToolUseFailure record), add
  three-strike guidance, and tell the git hook which session is committing.

Git hooks work for every agent (Codex, Gemini CLI, Cursor, Aider) and humans.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import checklist, ledger
from .config import Check, Config, load
from .gitio import GitError, Repo, atomic_write, git
from .runner import record, strike_message
from .verdict import Verdict, evaluate, stamp_value

TRAILER = "Tested-Tree"
MARK = "# installed by testedtree"
GIT_HOOKS = ("pre-commit", "commit-msg")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _age(at: str) -> str:
    try:
        t = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        s = max(0, int(time.time() - t))
    except (ValueError, OverflowError):
        return "?"
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def explain(v: Verdict, cfg: Config) -> list[str]:
    """One line per check, the same wording everywhere (status, gate, hooks)."""
    req = set(cfg.require)
    out: list[str] = []
    for c in v.checks:
        tag = "" if c.check in req else " (optional)"
        if c.state == "covered":
            if c.evidence is None:
                out.append(f"  {c.check}{tag}: ok (nothing changed)")
            else:
                extra = f"; {c.other_diffs} other path(s) differed at the time" if c.other_diffs else ""
                out.append(f"  {c.check}{tag}: COVERED by a pass on tree {c.evidence.tree[:12]} ({_age(c.evidence.at)}){extra}")
        elif c.state == "stale" and c.evidence is not None:
            shown = ", ".join(c.stale_paths[:5]) + (" …" if len(c.stale_paths) > 5 else "")
            out.append(
                f"  {c.check}{tag}: STALE, passed on tree {c.evidence.tree[:12]} ({_age(c.evidence.at)}) "
                f"but {len(c.stale_paths)} file(s) you are committing changed since: {shown}"
            )
        else:
            out.append(f"  {c.check}{tag}: MISSING, no passing run recorded")
        if c.state != "covered" and c.last_failure is not None:
            code = "timeout" if c.last_failure.exit is None else f"exit {c.last_failure.exit}"
            out.append(f"    last run FAILED ({code}, {_age(c.last_failure.at)})")
    return out


def checklist_lines(repo: Repo, cfg: Config, parent_tree: str, tree: str) -> list[str]:
    try:
        items, ref = checklist.load(repo, cfg.checklist_file, cfg.checklist_base)
    except checklist.ChecklistError as exc:
        return [f"testedtree: checklist error: {exc}"]
    if not items:
        return []
    hits = checklist.match(items, checklist.parse_diff(repo.diff_text(parent_tree, tree)))
    if not hits:
        return []
    lines = [f"testedtree: {len(hits)} of {len(items)} checklist items match this diff (from {ref}):"]
    for h in hits:
        lines.append(f"  [ ] {h.item.id}: {h.item.check}")
    return lines


# -- git hooks -----------------------------------------------------------------

def pre_commit(repo: Repo) -> int:
    cfg = load(repo)
    if cfg.mode == "off" or not cfg.checks:
        return 0
    try:
        tree = repo.staged_tree()
    except GitError as exc:
        _err(f"testedtree: cannot read the staged tree ({exc.stderr.strip()}); not checking")
        return 0
    parent = repo.head_tree()
    v = evaluate(repo, cfg, parent, tree)
    hint = ledger.read_session_hint(repo)
    session = hint[0] if hint else os.environ.get("TESTEDTREE_SESSION", "git")
    skip = os.environ.get("TESTEDTREE_SKIP", "").strip()
    for note in cfg.notes:
        _err(f"testedtree: note: {note}")
    cl = checklist_lines(repo, cfg, parent, tree)

    if v.ok:
        ledger.write_decision(repo, tree, {"decision": "pass"})
        names = ", ".join(c.check for c in v.covered()) or "nothing to check"
        _err(f"testedtree: ok, the committed files were tested as they are ({names})")
        for ln in cl:
            _err(ln)
        return 0

    if skip:
        ledger.write_decision(repo, tree, {"decision": "override", "override": f"skipped: {skip[:80]}"})
        _err(f"testedtree: not tested, committing anyway because TESTEDTREE_SKIP={skip!r}; stamped as none")
        return 0

    header = (
        "testedtree: WARNING (observe mode), the files you are committing were not tested as they are:"
        if cfg.mode == "observe"
        else "testedtree: COMMIT BLOCKED, the files you are committing were not tested as they are:"
    )
    todo = sorted(set(v.missing) | set(v.stale))
    fix = f"  fix: run `testedtree run {' '.join(todo)}` and commit again (other runs of these checks do not count)"

    if cfg.mode == "observe":
        ledger.write_decision(repo, tree, {"decision": "warn"})
        for ln in [header, *explain(v, cfg), fix, *cl]:
            _err(ln)
        return 0

    blocks = ledger.blocks_for(repo, session)
    if cfg.escape_after and blocks >= cfg.escape_after:
        ledger.write_decision(repo, tree, {"decision": "override", "override": f"override after {blocks} blocks"})
        _err(f"testedtree: still not tested after {blocks} blocks in this session; letting it through, stamped as an override")
        for ln in explain(v, cfg):
            _err(ln)
        return 0
    n = ledger.add_block(repo, session)
    ledger.write_decision(repo, tree, {"decision": "block"})
    for ln in [header, *explain(v, cfg), fix, *cl, f"  (block {n} for this session)"]:
        _err(ln)
    return 1


def _comment_char(repo: Repo) -> str:
    out = git(["config", "--get", "core.commentChar"], repo.root, check=False).strip()
    return out[:1] if out and out != "auto" else "#"


def commit_msg(repo: Repo, msg_file: Path) -> int:
    cfg = load(repo)
    if cfg.mode == "off" or not cfg.checks:
        return 0
    try:
        text = msg_file.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return 0
    cc = _comment_char(repo)
    body = [ln for ln in text.splitlines() if not ln.startswith(cc)]
    if not "".join(body).strip():
        return 0  # git is about to abort an empty message; don't make it non-empty
    try:
        tree = repo.staged_tree()
    except GitError:
        return 0
    v = evaluate(repo, cfg, repo.head_tree(), tree)
    decision = ledger.read_decision(repo, tree) or {}
    override = decision.get("override") if (not v.ok and decision.get("decision") == "override") else None
    value = stamp_value(v, override=override if isinstance(override, str) else None)
    git(
        ["interpret-trailers", "--in-place", "--if-exists", "replace", "--trailer", f"{TRAILER}: {value}", str(msg_file)],
        repo.root,
    )
    return 0


# -- Claude Code adapter -------------------------------------------------------

_COMMIT_RE = re.compile(r"(?:^|[;&|(]\s*|\s)git(?:\s+-[Cc]\s+\S+)*\s+commit(?![\w-])")
_ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_REDIRECT = re.compile(r"\s*(?:\d*>>?|<)\s*(?:&\d+|\S+)")


def is_git_commit(command: str) -> bool:
    return bool(_COMMIT_RE.search(command))


def matched_checks(cfg: Config, command: str) -> list[Check]:
    """Checks this exact agent command runs, or [] if the exit status could lie.

    Only ``&&`` chains are accepted: with ``;``, ``||``, a pipe or a background
    ``&`` the tool's exit code no longer tells us the check passed.
    """
    cmd = command.strip()
    if not cmd or "\n" in cmd:
        return []
    s = re.sub(r"\d*>&\d+|&>", " ", cmd)
    if re.search(r"\|\||[|;`]|\$\(", s):
        return []
    segs = [x.strip() for x in s.split("&&")]
    if any("&" in x for x in segs):
        return []
    hits: list[Check] = []
    for seg in segs:
        seg = _ENV_PREFIX.sub("", seg)
        seg = _REDIRECT.sub("", seg).strip()
        for chk in cfg.checks.values():
            if chk not in hits and chk.matches_command(seg):
                hits.append(chk)
    return hits


def _hook_output(event: str, context: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}})


def claude_hook(payload: dict[str, Any]) -> str | None:
    """Handle one Claude Code hook event. Returns JSON to print, or None."""
    event = payload.get("hook_event_name")
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command", ""))
    session = str(payload.get("session_id") or "claude")
    tuid = str(payload.get("tool_use_id") or "")
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    try:
        repo = Repo.find(cwd)
    except GitError:
        return None
    cfg = load(repo)
    if not cfg.checks:
        return None

    if event == "PreToolUse":
        if is_git_commit(command):
            ledger.write_session_hint(repo, session, "claude-code")
        checks = matched_checks(cfg, command)
        if checks and tuid:
            ledger.save_pending(
                repo, tuid, {"checks": [c.name for c in checks], "tree": repo.worktree_tree(), "start": time.time(), "command": command}
            )
        return None

    if event in ("PostToolUse", "PostToolUseFailure") and tuid:
        pend = ledger.pop_pending(repo, tuid)
        if not pend:
            return None
        ok = event == "PostToolUse"
        exit_code: int | None = 0
        if not ok:
            m = re.search(r"Exit code (\d+)", str(payload.get("error", "")))
            exit_code = int(m.group(1)) if m else None
        after = repo.worktree_tree()
        duration = int(payload.get("duration_ms") or (time.time() - float(pend.get("start", time.time()))) * 1000)
        msgs: list[str] = []
        for name in pend.get("checks", []):
            chk = cfg.checks.get(name)
            if chk is None:
                continue
            res = record(
                repo,
                chk,
                ok=ok,
                exit_code=exit_code,
                tree_before=str(pend["tree"]),
                tree_after=after,
                command=str(pend.get("command", command)),
                source="claude-hook",
                session=session,
                duration_ms=duration,
            )
            strike = None if ok else strike_message(name, res.strikes, cfg.strikes)
            if strike:
                msgs.append(strike)
        if msgs:
            return _hook_output(str(event), "\n".join(msgs))
    return None


# -- installers ----------------------------------------------------------------

def _launcher(hook: str, args: str) -> str:
    py = Path(sys.executable).as_posix()
    return f"""#!/bin/sh
{MARK} (https://github.com/Prime-agentai/testedtree)
prev="$(dirname "$0")/{hook}.pre-testedtree"
if [ -x "$prev" ]; then "$prev" "$@" || exit $?; fi
if [ -n "$TESTEDTREE_PYTHON" ]; then exec "$TESTEDTREE_PYTHON" -m testedtree git-hook {hook} {args}; fi
if command -v testedtree >/dev/null 2>&1; then exec testedtree git-hook {hook} {args}; fi
if [ -x {shlex.quote(py)} ]; then exec {shlex.quote(py)} -m testedtree git-hook {hook} {args}; fi
echo "testedtree: not found, so this commit was NOT checked (reinstall: testedtree install)" >&2
exit 0
"""


def install_git(repo: Repo) -> list[str]:
    hooks = repo.hooks_dir()
    hooks.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    for hook in GIT_HOOKS:
        path = hooks / hook
        if path.exists():
            current = path.read_text(encoding="utf-8", errors="replace")
            if MARK not in current:
                prev = hooks / f"{hook}.pre-testedtree"
                if prev.exists():
                    raise RuntimeError(f"{path} exists and so does {prev}; refusing to overwrite either")
                path.rename(prev)
                done.append(f"kept your existing {hook} hook: it now runs first, as {prev.name}")
        path.write_text(_launcher(hook, '"$@"'), encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        done.append(f"installed {path}")
    return done


def uninstall_git(repo: Repo) -> list[str]:
    hooks = repo.hooks_dir()
    done: list[str] = []
    for hook in GIT_HOOKS:
        path = hooks / hook
        if path.exists() and MARK in path.read_text(encoding="utf-8", errors="replace"):
            path.unlink()
            done.append(f"removed {path}")
            prev = hooks / f"{hook}.pre-testedtree"
            if prev.exists():
                prev.rename(path)
                done.append(f"restored your original {hook}")
    return done


CLAUDE_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def claude_settings(command: str = "testedtree hook claude") -> dict[str, Any]:
    entry = [{"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": 120}]}]
    return {"hooks": {ev: json.loads(json.dumps(entry)) for ev in CLAUDE_EVENTS}}


def merge_claude_settings(path: Path, command: str = "testedtree hook claude") -> bool:
    """Add our hooks to a Claude Code settings file. Returns False if already there."""
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"{path}: not a JSON object")
    hooks = data.setdefault("hooks", {})
    changed = False
    for ev, entries in claude_settings(command)["hooks"].items():
        lst = hooks.setdefault(ev, [])
        present = any(
            "testedtree hook claude" in str(h.get("command", ""))
            for e in lst
            if isinstance(e, dict)
            for h in e.get("hooks", [])
            if isinstance(h, dict)
        )
        if not present:
            lst.extend(entries)
            changed = True
    if changed:
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    return changed
