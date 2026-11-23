"""Thin, typed wrappers over the git plumbing testedtree needs.

Everything here talks to git through subprocess with an explicit argument list
(no shell). Trees are identified by git's own object ids, so evidence recorded
in one worktree is valid for identical content anywhere in the same repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed. ``stderr`` carries git's own explanation."""

    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({code}): {stderr.strip()}")
        self.code = code
        self.stderr = stderr


def git(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> str:
    """Run ``git <args>`` in ``cwd`` and return stdout (text, untouched)."""
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=full_env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc.stdout


@dataclass(frozen=True)
class Repo:
    """A git working tree plus the directories testedtree keeps state in."""

    root: Path
    git_dir: Path
    common_dir: Path

    @classmethod
    def find(cls, start: Path | None = None) -> "Repo":
        cwd = (start or Path.cwd()).resolve()
        try:
            out = git(
                ["rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
                cwd,
            ).splitlines()
        except GitError as exc:
            raise GitError(["rev-parse"], exc.code, f"{cwd} is not inside a git working tree") from exc
        root = Path(out[0])
        git_dir = Path(out[1])
        common = Path(out[2])
        if not common.is_absolute():
            common = (cwd / common).resolve()
        return cls(root=root, git_dir=git_dir, common_dir=common)

    # -- state directory -------------------------------------------------
    @property
    def state_dir(self) -> Path:
        """Shared by all worktrees: trees are content-addressed, so evidence is too."""
        d = self.common_dir / "testedtree"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run(self, args: list[str], **kw: object) -> str:
        return git(args, self.root, **kw)  # type: ignore[arg-type]

    # -- trees -----------------------------------------------------------
    def empty_tree(self) -> str:
        """The empty tree id, correct for both SHA-1 and SHA-256 repositories."""
        return self.run(["hash-object", "-t", "tree", "--stdin"], input_text="").strip()

    def head_commit(self) -> str | None:
        out = git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], self.root, check=False).strip()
        return out or None

    def tree_of(self, rev: str) -> str | None:
        out = git(["rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}"], self.root, check=False).strip()
        return out or None

    def head_tree(self) -> str:
        return self.tree_of("HEAD") or self.empty_tree()

    def staged_tree(self) -> str:
        """The tree ``git commit`` would record right now (fails on unmerged paths)."""
        return self.run(["write-tree"]).strip()

    def worktree_tree(self) -> str:
        """Snapshot the working tree (tracked + untracked, minus ignored) as a tree id.

        Uses a throwaway copy of the index, so the user's staging area is never
        touched. Copying the real index keeps git's stat cache, so this is an
        incremental ``git add -A``, not a full rehash. Blobs for untracked files
        are written to the object store as loose objects; ``git gc`` prunes them.
        """
        tmpdir = self.state_dir / "tmp"
        tmpdir.mkdir(exist_ok=True)
        tmp_index = tmpdir / f"index-{uuid.uuid4().hex}"
        real_index = self.git_dir / "index"
        try:
            if real_index.exists():
                shutil.copyfile(real_index, tmp_index)
            env = {"GIT_INDEX_FILE": str(tmp_index)}
            self.run(["add", "-A", "--", ":/"], env=env)
            return self.run(["write-tree"], env=env).strip()
        finally:
            try:
                tmp_index.unlink()
            except FileNotFoundError:
                pass

    def changed_paths(self, old_tree: str, new_tree: str) -> list[str]:
        """Paths whose blob (or mode, or presence) differs between two trees."""
        if old_tree == new_tree:
            return []
        out = self.run(["diff-tree", "-r", "-z", "--no-renames", "--name-only", old_tree, new_tree])
        return [p for p in out.split("\0") if p]

    def paths_differing(self, tree_a: str, tree_b: str, among: set[str]) -> list[str]:
        """Which of ``among`` differ between two trees (one git call, any size)."""
        if tree_a == tree_b or not among:
            return []
        return sorted(p for p in self.changed_paths(tree_a, tree_b) if p in among)

    def show_file(self, rev: str, path: str) -> str | None:
        """File contents at ``rev`` (commit or tree id), or None if absent there."""
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{rev}:{path}"], cwd=str(self.root), capture_output=True
        )
        if proc.returncode != 0:
            return None
        return self.run(["cat-file", "blob", f"{rev}:{path}"])

    def resolve(self, rev: str) -> str | None:
        out = git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], self.root, check=False).strip()
        return out or None

    def merge_base(self, a: str, b: str) -> str | None:
        out = git(["merge-base", a, b], self.root, check=False).strip()
        return out or None

    def diff_text(self, old_tree: str, new_tree: str) -> str:
        return self.run(
            [
                "-c",
                "core.quotepath=false",
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
                "-U0",
                old_tree,
                new_tree,
            ]
        )

    def hooks_dir(self) -> Path:
        out = git(["config", "--get", "core.hooksPath"], self.root, check=False).strip()
        if out:
            p = Path(out).expanduser()
            return p if p.is_absolute() else (self.root / p)
        return self.common_dir / "hooks"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
