"""``.testedtree.toml``: which checks exist, which are required, how strict the gate is.

The gate reads this file from the HEAD commit, not the working copy, so a change
can't weaken its own checks (redefining ``tests`` as ``true`` in an uncommitted
edit does nothing). Until the file is committed for the first time the working
copy is used, and every verdict says so.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .gitio import Repo

CONFIG_NAME = ".testedtree.toml"
DEFAULT_CHECKLIST = ".testedtree/checklist.toml"

Mode = Literal["off", "observe", "enforce"]
Coverage = Literal["paths", "exact"]
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    run: str
    match: tuple[str, ...] = ()
    mutates: bool = False
    timeout_s: float | None = None

    def matches_command(self, segment: str) -> bool:
        """True if one ``&&``-segment of an agent's shell command *is* this check.

        By default only the exact configured command counts: a narrowed run
        (``pytest tests/test_one.py``, ``-k smoke``) is not the check. Widen it
        on purpose with ``match`` regexes.
        """
        seg = " ".join(segment.split())
        if seg == " ".join(self.run.split()):
            return True
        return any(re.fullmatch(rx, seg) for rx in self.match)


@dataclass(frozen=True)
class Config:
    checks: dict[str, Check]
    require: tuple[str, ...]
    mode: Mode = "observe"
    coverage: Coverage = "paths"
    escape_after: int = 2
    strikes: int = 3
    checklist_file: str = DEFAULT_CHECKLIST
    checklist_base: str | None = None
    exempt: tuple[str, ...] = ()
    source: str = "HEAD"  # "HEAD", "worktree" or "default"
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required_checks(self) -> list[Check]:
        return [self.checks[n] for n in self.require]


def parse(text: str, source: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{CONFIG_NAME} ({source}): {exc}") from exc
    return from_dict(data, source)


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(f"{CONFIG_NAME}: {msg}")


def from_dict(data: dict[str, Any], source: str) -> Config:
    known_top = {"gate", "checks", "checklist"}
    unknown = set(data) - known_top
    _expect(not unknown, f"unknown table(s): {', '.join(sorted(unknown))}")

    raw_checks = data.get("checks", {})
    _expect(isinstance(raw_checks, dict), "[checks] must be a table of tables")
    checks: dict[str, Check] = {}
    for name, spec in raw_checks.items():
        _expect(bool(_NAME_RE.match(name)), f"bad check name {name!r}")
        _expect(isinstance(spec, dict), f"[checks.{name}] must be a table")
        bad = set(spec) - {"run", "match", "mutates", "timeout"}
        _expect(not bad, f"[checks.{name}] unknown key(s): {', '.join(sorted(bad))}")
        run = spec.get("run")
        _expect(isinstance(run, str) and run.strip() != "", f"[checks.{name}] needs run = \"<command>\"")
        match = spec.get("match", [])
        _expect(isinstance(match, list) and all(isinstance(m, str) for m in match), f"[checks.{name}] match must be a list of regexes")
        for rx in match:
            try:
                re.compile(rx)
            except re.error as exc:
                raise ConfigError(f"{CONFIG_NAME}: [checks.{name}] bad regex {rx!r}: {exc}") from exc
        mutates = spec.get("mutates", False)
        _expect(isinstance(mutates, bool), f"[checks.{name}] mutates must be true/false")
        timeout = spec.get("timeout")
        _expect(timeout is None or (isinstance(timeout, (int, float)) and timeout > 0), f"[checks.{name}] timeout must be seconds > 0")
        checks[name] = Check(name, str(run), tuple(match), mutates, float(timeout) if timeout else None)

    gate = data.get("gate", {})
    _expect(isinstance(gate, dict), "[gate] must be a table")
    bad = set(gate) - {"mode", "require", "coverage", "escape_after", "strikes", "exempt"}
    _expect(not bad, f"[gate] unknown key(s): {', '.join(sorted(bad))}")
    mode = gate.get("mode", "observe")
    _expect(mode in ("off", "observe", "enforce"), "[gate] mode must be off, observe or enforce")
    coverage = gate.get("coverage", "paths")
    _expect(coverage in ("paths", "exact"), "[gate] coverage must be paths or exact")
    require = gate.get("require", list(checks)[:1])
    _expect(isinstance(require, list) and all(isinstance(r, str) for r in require), "[gate] require must be a list of check names")
    for r in require:
        _expect(r in checks, f"[gate] require names unknown check {r!r}")
    escape_after = gate.get("escape_after", 2)
    _expect(isinstance(escape_after, int) and escape_after >= 0, "[gate] escape_after must be an integer >= 0 (0 = never)")
    strikes = gate.get("strikes", 3)
    _expect(isinstance(strikes, int) and strikes >= 0, "[gate] strikes must be an integer >= 0 (0 = off)")

    exempt = gate.get("exempt", [])
    _expect(isinstance(exempt, list) and all(isinstance(e, str) for e in exempt), "[gate] exempt must be a list of globs")

    cl = data.get("checklist", {})
    _expect(isinstance(cl, dict), "[checklist] must be a table")
    bad = set(cl) - {"file", "base"}
    _expect(not bad, f"[checklist] unknown key(s): {', '.join(sorted(bad))}")
    cl_file = cl.get("file", DEFAULT_CHECKLIST)
    cl_base = cl.get("base")
    _expect(isinstance(cl_file, str), "[checklist] file must be a path")
    _expect(cl_base is None or isinstance(cl_base, str), "[checklist] base must be a ref name")

    return Config(
        checks=checks,
        require=tuple(require),
        mode=mode,
        coverage=coverage,
        escape_after=escape_after,
        strikes=strikes,
        checklist_file=cl_file,
        checklist_base=cl_base,
        exempt=tuple(exempt),
        source=source,
    )


def load(repo: Repo, *, from_worktree: bool = False) -> Config:
    """Committed config (HEAD) by default; the working copy only before first commit."""
    if not from_worktree and repo.head_commit():
        text = repo.show_file("HEAD", CONFIG_NAME)
        if text is not None:
            return parse(text, "HEAD")
    path = repo.root / CONFIG_NAME
    if path.exists():
        cfg = parse(path.read_text(encoding="utf-8"), "worktree")
        if from_worktree:
            return cfg
        note = f"{CONFIG_NAME} is not committed yet: using the working copy, which this change can still edit"
        return dataclasses.replace(cfg, notes=(*cfg.notes, note))
    return Config(checks={}, require=(), mode="off", source="default", notes=(f"no {CONFIG_NAME}: run `testedtree init`",))


def detect_checks(root: Path) -> dict[str, str]:
    """Best-guess check commands for ``init`` (only things we can see evidence for)."""
    found: dict[str, str] = {}
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "setup.cfg").exists():
        if (root / "tests").is_dir() or (root / "test").is_dir() or (root / "pytest.ini").exists():
            found["tests"] = "python -m pytest -q"
    if "tests" not in found and (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            if isinstance(pkg.get("scripts"), dict) and "test" in pkg["scripts"]:
                found["tests"] = "npm test"
        except (ValueError, OSError):
            pass
    if "tests" not in found and (root / "go.mod").exists():
        found["tests"] = "go test ./..."
    if "tests" not in found and (root / "Cargo.toml").exists():
        found["tests"] = "cargo test"
    return found


def render_default(checks: dict[str, str]) -> str:
    if not checks:
        checks = {"tests": "make test"}
    lines = [
        "# testedtree: a commit passes the gate only if every required check passed",
        "# on a tree where the committed files are byte-identical.",
        "# This file is read from HEAD, so edits take effect after they are committed.",
        "",
        "[gate]",
        'mode = "observe"        # off | observe (warn + stamp) | enforce (block)',
        f"require = {json.dumps(list(checks))}",
        'coverage = "paths"      # paths: changed files identical | exact: whole tree identical',
        "escape_after = 2        # enforce: let a session through after N blocks (stamped as override)",
        "strikes = 3             # say 'stop and ask a human' after N consecutive failures",
        'exempt = ["*.md", "docs/**"]   # changes only here need no checks (they still get a stamp)',
        "",
    ]
    for name, run in checks.items():
        lines += [f"[checks.{name}]", f"run = {json.dumps(run)}","# match = ['^uv run pytest( -q)?$']   # extra agent commands that count as this check", ""]
    lines += ["[checklist]", f'file = "{DEFAULT_CHECKLIST}"', '# base = "origin/main"   # read the checklist from here (default: HEAD)', ""]
    return "\n".join(lines)
