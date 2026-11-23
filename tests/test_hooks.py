"""Real ``git commit`` through installed hooks, and the Claude Code adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import PY, Sandbox

from testedtree import config, hooks, ledger
from testedtree.gitio import Repo


def test_enforce_blocks_untested_commit_then_passes_and_stamps(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "print('v2')\n")
    r = sb.commit_all("v2")
    assert r.returncode != 0
    assert "COMMIT BLOCKED" in r.stderr and "tests: MISSING" in r.stderr
    assert "testedtree run tests" in r.stderr
    assert sb.tt("run", "tests").returncode == 0
    r = sb.commit_all("v2")
    assert r.returncode == 0, r.stderr
    msg = sb.last_message()
    assert "Tested-Tree: tests@" in msg
    stamped = msg.split("Tested-Tree: tests@")[1].split()[0]
    assert sb.git("rev-parse", "HEAD^{tree}").stdout.startswith(stamped)


def test_edit_after_tests_is_blocked_as_stale_naming_the_file(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "print('v2')\n")
    sb.tt("run", "tests")
    sb.write("app.py", "print('v2 plus a last-minute tweak')\n")
    r = sb.commit_all("v2")
    assert r.returncode != 0
    assert "tests: STALE" in r.stderr and "app.py" in r.stderr


def test_escape_after_two_blocks_is_stamped_as_override(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "print('v2')\n")
    assert sb.commit_all("v2").returncode != 0
    assert sb.commit_all("v2").returncode != 0
    r = sb.commit_all("v2")
    assert r.returncode == 0, r.stderr
    assert "Tested-Tree: none (override after 2 blocks; missing: tests)" in sb.last_message()


def test_skip_env_is_stamped_with_the_reason(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("README.md", "docs\n")
    r = sb.commit_all("docs", env={"TESTEDTREE_SKIP": "docs only"})
    assert r.returncode == 0
    assert "Tested-Tree: none (skipped: docs only; missing: tests)" in sb.last_message()


def test_observe_mode_warns_and_stamps_none(sb: Sandbox) -> None:
    sb.config("observe")
    sb.commit_all("adopt")
    sb.tt("install")
    sb.write("app.py", "print('v2')\n")
    r = sb.commit_all("v2")
    assert r.returncode == 0 and "WARNING (observe mode)" in r.stderr
    assert "Tested-Tree: none (missing: tests)" in sb.last_message()


def test_off_mode_does_nothing(sb: Sandbox) -> None:
    sb.config("off")
    sb.commit_all("adopt")
    sb.tt("install")
    sb.write("app.py", "print('v2')\n")
    r = sb.commit_all("v2")
    assert r.returncode == 0 and "Tested-Tree" not in sb.last_message()


def test_uncommitted_config_edit_cannot_weaken_the_gate(adopted: Sandbox) -> None:
    sb = adopted
    text = (sb.root / ".testedtree.toml").read_text()
    sb.write(".testedtree.toml", text.replace('mode = "enforce"', 'mode = "off"'))
    sb.write("app.py", "print('v2')\n")
    r = sb.commit_all("sneaky")
    assert r.returncode != 0 and "COMMIT BLOCKED" in r.stderr


def test_commit_dash_a_judges_what_is_actually_committed(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "print('v2')\n")
    sb.tt("run", "tests")
    r = sb.git("commit", "-a", "-m", "v2", check=False)
    assert r.returncode == 0, r.stderr
    assert "Tested-Tree: tests@" in sb.last_message()


def test_existing_hook_is_chained_and_restored(sb: Sandbox) -> None:
    sb.config("enforce")
    sb.commit_all("adopt")
    hook = sb.root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho mine >&2\nexit 0\n")
    hook.chmod(0o755)
    out = sb.tt("install").stdout
    assert "kept your existing pre-commit" in out
    sb.write("app.py", "print('v2')\n")
    sb.tt("run", "tests")
    r = sb.commit_all("v2")
    assert r.returncode == 0 and "mine" in r.stderr and "testedtree: ok" in r.stderr
    sb.tt("uninstall")
    assert hook.read_text() == "#!/bin/sh\necho mine >&2\nexit 0\n"
    assert not (sb.root / ".git" / "hooks" / "commit-msg").exists()


def test_install_is_idempotent(adopted: Sandbox) -> None:
    out = adopted.tt("install").stdout
    assert "kept your existing" not in out
    assert not (adopted.root / ".git" / "hooks" / "pre-commit.pre-testedtree").exists()


def test_empty_message_is_not_kept_alive_by_the_trailer(adopted: Sandbox) -> None:
    sb = adopted
    sb.write("app.py", "print('v2')\n")
    sb.tt("run", "tests")
    sb.git("add", "-A")
    r = sb.git("commit", "--allow-empty-message", "-m", "", check=False)
    # git accepted the empty message on purpose; we just must not have added a trailer to it
    assert r.returncode == 0
    assert "Tested-Tree" not in sb.last_message()


def test_checklist_items_are_shown_from_head(adopted: Sandbox) -> None:
    sb = adopted
    sb.write(".testedtree/checklist.toml", '[[item]]\nid = "app-entry"\npaths = ["app.py"]\ncheck = "Did you run the app by hand?"\n')
    sb.commit_all("checklist", env={"TESTEDTREE_SKIP": "policy"})
    sb.write("app.py", "print('v2')\n")
    sb.tt("run", "tests")
    r = sb.commit_all("v2")
    assert "1 of 1 checklist items match" in r.stderr and "app-entry" in r.stderr


# -- Claude Code adapter -------------------------------------------------------

def _payload(sb: Sandbox, event: str, command: str, tuid: str = "toolu_1", session: str = "sess-A", **extra: object) -> dict[str, object]:
    return {
        "session_id": session,
        "cwd": str(sb.root),
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": tuid,
        **extra,
    }


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("{PY} check_app.py", ["tests"]),
        ("cd sub && {PY} check_app.py", ["tests"]),
        ("CI=1 {PY} check_app.py > out.log 2>&1", ["tests"]),
        ("{PY} check_app.py && {PY} lint_app.py", ["tests", "lint"]),
        ("{PY} check_app.py || true", []),
        ("{PY} check_app.py; true", []),
        ("{PY} check_app.py | tail -5", []),
        ("{PY} check_app.py &", []),
        ("{PY} check_app.py -k smoke", []),
        ("echo {PY} check_app.py", []),
    ],
)
def test_matched_checks_only_trust_exit_codes_that_mean_pass(sb: Sandbox, command: str, expected: list[str]) -> None:
    sb.config("enforce")
    cfg = config.load(Repo.find(sb.root), from_worktree=True)
    assert [c.name for c in hooks.matched_checks(cfg, command.replace("{PY}", PY))] == expected


def test_match_regexes_widen_on_purpose() -> None:
    cfg = config.parse('[checks.tests]\nrun = "pytest -q"\nmatch = [\'(uv run )?pytest( -q)?\']\n', "t")
    assert [c.name for c in hooks.matched_checks(cfg, "uv run pytest")] == ["tests"]
    assert hooks.matched_checks(cfg, "pytest tests/test_one.py") == []


def test_is_git_commit() -> None:
    assert hooks.is_git_commit("git commit -m x")
    assert hooks.is_git_commit("git add -A && git -C repo commit -m x")
    assert not hooks.is_git_commit("git commit-tree abc")
    assert not hooks.is_git_commit("git log --grep commit")


def test_claude_hook_records_pass_bound_to_the_tree_before_the_run(adopted: Sandbox) -> None:
    sb = adopted
    repo = Repo.find(sb.root)
    sb.write("app.py", "print('agent edit')\n")
    cmd = f"{PY} check_app.py"
    assert hooks.claude_hook(_payload(sb, "PreToolUse", cmd)) is None
    tree = repo.worktree_tree()
    assert hooks.claude_hook(_payload(sb, "PostToolUse", cmd, tool_response={"stdout": ""}, duration_ms=12)) is None
    ev = ledger.records(repo)[-1]
    assert (ev.check, ev.status, ev.tree, ev.source, ev.session) == ("tests", "pass", tree, "claude-hook", "sess-A")
    r = sb.commit_all("agent change")
    assert r.returncode == 0, r.stderr


def test_claude_hook_failure_records_exit_code_and_strikes_out_at_three(adopted: Sandbox) -> None:
    sb = adopted
    repo = Repo.find(sb.root)
    sb.write("app.py", "BUG\n")
    cmd = f"{PY} check_app.py"
    outs = []
    for i in range(3):
        hooks.claude_hook(_payload(sb, "PreToolUse", cmd, tuid=f"t{i}"))
        outs.append(hooks.claude_hook(_payload(sb, "PostToolUseFailure", cmd, tuid=f"t{i}", error="Exit code 1")))
    assert outs[0] is None and outs[1] is None
    assert outs[2] is not None
    out = json.loads(outs[2])["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUseFailure"
    assert "failed 3 times in a row" in out["additionalContext"] and "ask a human" in out["additionalContext"]
    assert [e.exit for e in ledger.records(repo)] == [1, 1, 1]
    # another session starts counting from zero (measured live: a carried-over count confused the agent)
    hooks.claude_hook(_payload(sb, "PreToolUse", cmd, tuid="t9", session="sess-B"))
    assert hooks.claude_hook(_payload(sb, "PostToolUseFailure", cmd, tuid="t9", session="sess-B", error="Exit code 1")) is None


def test_claude_hook_ignores_unrelated_commands_and_tools(adopted: Sandbox) -> None:
    sb = adopted
    repo = Repo.find(sb.root)
    hooks.claude_hook(_payload(sb, "PreToolUse", "ls -la"))
    hooks.claude_hook(_payload(sb, "PostToolUse", "ls -la"))
    p = _payload(sb, "PreToolUse", f"{PY} check_app.py")
    p["tool_name"] = "Read"
    hooks.claude_hook(p)
    assert ledger.records(repo) == []


def test_claude_session_hint_makes_escape_per_session(adopted: Sandbox) -> None:
    sb = adopted
    repo = Repo.find(sb.root)
    sb.write("app.py", "print('v2')\n")
    for _ in range(2):
        hooks.claude_hook(_payload(sb, "PreToolUse", "git commit -m v2", session="sess-A"))
        assert sb.commit_all("v2").returncode != 0
    assert ledger.blocks_for(repo, "sess-A") == 2
    # a different session starts from zero
    hooks.claude_hook(_payload(sb, "PreToolUse", "git commit -m v2", session="sess-B"))
    assert sb.commit_all("v2").returncode != 0
    hooks.claude_hook(_payload(sb, "PreToolUse", "git commit -m v2", session="sess-A"))
    assert sb.commit_all("v2").returncode == 0
    assert "override after 2 blocks" in sb.last_message()


def test_hook_cli_never_breaks_the_agent(adopted: Sandbox) -> None:
    r = adopted.tt("hook", "claude", stdin="not json")
    assert r.returncode == 0 and "internal error, ignored" in r.stderr
    r = adopted.tt("hook", "claude", stdin=json.dumps(_payload(adopted, "PreToolUse", "ls")))
    assert r.returncode == 0 and r.stdout == ""


def test_claude_settings_merge_is_idempotent_and_keeps_existing(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "mine"}]}]}}))
    assert hooks.merge_claude_settings(path) is True
    assert hooks.merge_claude_settings(path) is False
    data = json.loads(path.read_text())
    assert data["model"] == "x"
    assert len(data["hooks"]["PreToolUse"]) == 2
    assert set(data["hooks"]) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
