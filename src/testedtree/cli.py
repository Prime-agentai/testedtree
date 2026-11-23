"""Command line: ``testedtree <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, checklist, hooks, ledger, report
from .config import CONFIG_NAME, Config, ConfigError, detect_checks, load, render_default
from .gitio import GitError, Repo, atomic_write
from .runner import describe, run_check
from .verdict import Verdict, evaluate, stamp_value


def _repo() -> Repo:
    return Repo.find(Path.cwd())


def cmd_init(a: argparse.Namespace) -> int:
    repo = _repo()
    cfg_path = repo.root / CONFIG_NAME
    if cfg_path.exists() and not a.force:
        print(f"{CONFIG_NAME} already exists (use --force to overwrite)", file=sys.stderr)
    else:
        checks = detect_checks(repo.root)
        if a.check:
            checks = dict(c.split("=", 1) for c in a.check)
        atomic_write(cfg_path, render_default(checks))
        print(f"wrote {CONFIG_NAME} with checks: {', '.join(checks) or 'tests (edit the command!)'}")
    cl = repo.root / ".testedtree" / "checklist.toml"
    if not cl.exists():
        atomic_write(cl, checklist.EXAMPLE)
        print(f"wrote {cl.relative_to(repo.root)} (4 example items; edit them)")
    print("next: commit these two files, then `testedtree install` (git hooks) and optionally `testedtree install claude`")
    return 0


def cmd_install(a: argparse.Namespace) -> int:
    repo = _repo()
    if a.target == "git":
        for line in hooks.install_git(repo):
            print(line)
        return 0
    command = a.command or "testedtree hook claude"
    if a.print:
        print(json.dumps(hooks.claude_settings(command), indent=2))
        return 0
    path = Path(a.settings) if a.settings else repo.root / ".claude" / "settings.json"
    changed = hooks.merge_claude_settings(path, command)
    print(f"{'added testedtree hooks to' if changed else 'already present in'} {path}")
    return 0


def cmd_uninstall(a: argparse.Namespace) -> int:
    for line in hooks.uninstall_git(_repo()) or ["nothing to remove"]:
        print(line)
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load(repo)
    if not cfg.checks:
        print(f"testedtree: no checks configured ({'; '.join(cfg.notes)})", file=sys.stderr)
        return 2
    names = a.checks or (list(cfg.require) if cfg.require and not a.all else list(cfg.checks))
    unknown = [n for n in names if n not in cfg.checks]
    if unknown:
        print(f"testedtree: unknown check(s): {', '.join(unknown)} (configured: {', '.join(cfg.checks)})", file=sys.stderr)
        return 2
    for note in cfg.notes:
        print(f"testedtree: note: {note}", file=sys.stderr)
    first_fail = 0
    for name in names:
        res = run_check(repo, cfg.checks[name], session=a.session, quiet=a.quiet)
        print(describe(res, cfg), file=sys.stderr)
        if res.evidence.status == "fail":
            code = res.evidence.exit
            first_fail = first_fail or (code if code else 1)
            if not a.keep_going:
                break
    return int(first_fail)


def _verdict_for(repo: Repo, staged: bool) -> tuple[Config, Verdict]:
    cfg = load(repo)
    tree = repo.staged_tree() if staged else repo.worktree_tree()
    return cfg, evaluate(repo, cfg, repo.head_tree(), tree)


def cmd_status(a: argparse.Namespace) -> int:
    repo = _repo()
    cfg, v = _verdict_for(repo, a.staged)
    if a.json:
        print(json.dumps({
            "ok": v.ok, "tree": v.tree, "parent_tree": v.parent_tree, "changed": list(v.changed),
            "mode": cfg.mode, "config_source": cfg.source, "stamp": stamp_value(v),
            "checks": [{"check": c.check, "state": c.state, "required": c.check in cfg.require,
                        "evidence_tree": c.evidence.tree if c.evidence else None,
                        "stale_paths": list(c.stale_paths)} for c in v.checks],
        }, indent=2))
    else:
        what = "staged changes" if a.staged else "working tree"
        print(f"testedtree: {what} vs HEAD: {len(v.changed)} changed file(s), tree {v.tree[:12]}, mode {cfg.mode}")
        for note in cfg.notes:
            print(f"  note: {note}")
        for line in hooks.explain(v, cfg):
            print(line)
        print(f"  would stamp: {hooks.TRAILER}: {stamp_value(v)}")
        if not v.ok:
            todo = sorted(set(v.missing) | set(v.stale))
            print(f"  next: testedtree run {' '.join(todo)}")
    return 1 if (a.strict and not v.ok) else 0


def cmd_gate(a: argparse.Namespace) -> int:
    repo = _repo()
    cfg, v = _verdict_for(repo, not a.worktree)
    for line in hooks.explain(v, cfg):
        print(line, file=sys.stderr)
    print("PASS" if v.ok else "FAIL", file=sys.stderr)
    return 0 if v.ok else 1


def _range_trees(repo: Repo, rng: str | None, staged: bool) -> tuple[str, str]:
    if rng:
        if "..." in rng:
            a, b = rng.split("...", 1)
            b = b or "HEAD"
            base = repo.merge_base(a, b)
            if base is None:
                raise GitError(["merge-base"], 1, f"no merge base for {rng}")
            return repo.tree_of(base) or repo.empty_tree(), repo.tree_of(b) or repo.empty_tree()
        if ".." in rng:
            a, b = rng.split("..", 1)
            return repo.tree_of(a) or repo.empty_tree(), repo.tree_of(b or "HEAD") or repo.empty_tree()
        return repo.tree_of(rng) or repo.empty_tree(), repo.tree_of("HEAD") or repo.empty_tree()
    return repo.head_tree(), repo.staged_tree() if staged else repo.worktree_tree()


def cmd_checklist(a: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load(repo)
    items, ref = checklist.load(repo, a.file or cfg.checklist_file, a.base or cfg.checklist_base)
    old, new = _range_trees(repo, a.range, a.staged)
    hits = checklist.match(items, checklist.parse_diff(repo.diff_text(old, new)))
    if a.format == "json":
        print(json.dumps({"ref": ref, "total": len(items), "hits": [
            {"id": h.item.id, "check": h.item.check, "why": h.item.why, "paths": list(h.paths), "lines": list(h.lines)} for h in hits
        ]}, indent=2))
    else:
        print(checklist.render_md(hits, ref, len(items)))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    repo = _repo()
    cfg = load(repo)
    rows = report.collect(repo, cfg, a.range, a.limit)
    try:
        items, _ = checklist.load(repo, cfg.checklist_file, cfg.checklist_base)
    except checklist.ChecklistError:
        items = []
    sys.stdout.write(report.render(rows, items, a.format, a.range))
    if a.fail_on:
        bad = {"none": {"none", "override", "skipped"}, "unstamped": {"none", "override", "skipped", "unstamped"}}[a.fail_on]
        if any(r.kind in bad and not r.merge for r in rows):
            return 1
    return 0


def cmd_hook(a: argparse.Namespace) -> int:
    # Never break the agent: any internal error becomes a note on stderr, exit 0.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out = hooks.claude_hook(payload) if isinstance(payload, dict) else None
        if out:
            print(out)
    except Exception as exc:  # noqa: BLE001
        print(f"testedtree hook: internal error, ignored: {exc!r}", file=sys.stderr)
    return 0


def cmd_git_hook(a: argparse.Namespace) -> int:
    try:
        repo = _repo()
        if a.hook == "pre-commit":
            return hooks.pre_commit(repo)
        if a.hook == "commit-msg" and a.args:
            return hooks.commit_msg(repo, Path(a.args[0]))
    except (ConfigError, checklist.ChecklistError) as exc:
        print(f"testedtree: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_ledger(a: argparse.Namespace) -> int:
    repo = _repo()
    for ev in ledger.records(repo)[-a.n:]:
        code = "timeout" if ev.exit is None else ev.exit
        print(f"{ev.at[:19]}  {ev.check:<10} {ev.status:<4} exit={code!s:<7} tree={ev.tree[:12]}{'*' if ev.drifted else ' '} {ev.source:<11} {ev.command[:60]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="testedtree", description="Only commit the tree you tested.")
    p.add_argument("--version", action="version", version=f"testedtree {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help=f"write {CONFIG_NAME} (detected checks) and an example checklist")
    s.add_argument("--check", action="append", metavar="NAME=COMMAND", help="define a check instead of detecting")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("install", help="install git hooks (default) or Claude Code hooks")
    s.add_argument("target", nargs="?", choices=["git", "claude"], default="git")
    s.add_argument("--settings", help="Claude Code settings file to merge into (default: <repo>/.claude/settings.json)")
    s.add_argument("--print", action="store_true", help="print the Claude Code hooks JSON instead of writing it")
    s.add_argument("--command", help="hook command (default: testedtree hook claude)")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("uninstall", help="remove the git hooks (restores any you had)")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("run", help="run checks and record tree-bound evidence")
    s.add_argument("checks", nargs="*")
    s.add_argument("--all", action="store_true", help="every configured check, not just required ones")
    s.add_argument("--keep-going", action="store_true")
    s.add_argument("--quiet", action="store_true", help="hide the check's own output")
    s.add_argument("--session", help="attribute the run to an agent session id")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="would the current changes pass the gate?")
    s.add_argument("--staged", action="store_true", help="judge the staged tree, not the working tree")
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true", help="exit 1 when not covered")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("gate", help="exit 1 unless the staged tree is covered (ignores mode)")
    s.add_argument("--worktree", action="store_true", help="judge the working tree instead")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("checklist", help="checklist items this diff triggers")
    s.add_argument("--range", help="A..B, A...B (PR style) or a base ref; default: HEAD vs working tree")
    s.add_argument("--staged", action="store_true")
    s.add_argument("--base", help="ref to read the checklist from")
    s.add_argument("--file", help="checklist path inside that ref")
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.set_defaults(fn=cmd_checklist)

    s = sub.add_parser("report", help="share of commits tested as committed, from trailers")
    s.add_argument("--range", default="HEAD")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--format", choices=["text", "md", "json"], default="text")
    s.add_argument("--fail-on", choices=["none", "unstamped"], help="exit 1 if any non-merge commit is untested")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("ledger", help="show recent evidence records")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("hook", help="agent hook adapter (reads the hook payload on stdin)")
    s.add_argument("agent", choices=["claude"])
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("git-hook", help=argparse.SUPPRESS)
    s.add_argument("hook", choices=list(hooks.GIT_HOOKS))
    s.add_argument("args", nargs="*")
    s.set_defaults(fn=cmd_git_hook)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (GitError, ConfigError, checklist.ChecklistError, RuntimeError, ValueError) as exc:
        print(f"testedtree: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
