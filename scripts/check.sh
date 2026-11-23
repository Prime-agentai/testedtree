#!/usr/bin/env bash
# The one gate: types, tests, then the CLI end to end in a scratch repo.
set -euo pipefail
trap 'echo "check.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR

PY="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export TESTEDTREE_PYTHON="$PY"

echo "== mypy --strict"
"$PY" -m mypy --strict "$ROOT/src"

echo "== pytest"
"$PY" -m pytest -q "$ROOT/tests"

echo "== CLI end to end"
WORK="$(mktemp -d)"
LOGS="$(mktemp -d)"
cd "$WORK"
git init -q -b main
git config user.email dev@example.com
git config user.name Dev
git config commit.gpgsign false
tt() { "$PY" -m testedtree "$@"; }
# Native paths: on Windows the check runs under cmd.exe and the hook payload is read by native Python.
NPY="$("$PY" -c 'import sys, pathlib; print(pathlib.Path(sys.executable).as_posix())')"
NWORK="$("$PY" -c 'import os; print(os.getcwd().replace(chr(92), "/"))')"

printf 'def greet():\n    return "hello"\n' > app.py
printf 'from app import greet\nimport sys\nsys.exit(0 if greet().startswith("h") else 1)\n' > check_app.py
printf '__pycache__/\n' > .gitignore
tt init --check "tests=\"$NPY\" check_app.py"
sed -i.bak 's/^mode = "observe"/mode = "enforce"/' .testedtree.toml && rm .testedtree.toml.bak
git add -A && git commit -qm "adopt testedtree"
tt install

# 1. untested change is blocked
printf 'def greet():\n    return "hi there"\n' > app.py
git add -A
if git commit -qm "untested" 2> "$LOGS/blocked.log"; then echo "expected a block" >&2; exit 1; fi
grep -q "COMMIT BLOCKED" "$LOGS/blocked.log"
grep -q "testedtree run tests" "$LOGS/blocked.log"

# 2. run the check, commit, and the stamp names the tested tree (here: exactly HEAD's tree)
tt run tests
git commit -qm "tested greeting"
git log -1 --format=%B | grep -q "^Tested-Tree: tests@"
stamp="$(git log -1 --format='%(trailers:key=Tested-Tree,valueonly)' | sed 's/^tests@//' | tr -d '[:space:]')"
case "$(git rev-parse 'HEAD^{tree}')" in "$stamp"*) ;; *) echo "stamp $stamp is not HEAD's tree" >&2; exit 1 ;; esac

# 3. edit after testing is stale
printf 'def greet():\n    return "hey"\n' > app.py
tt run tests
printf 'def greet():\n    return "howdy"\n' > app.py
tt status | grep -q "STALE"

# 4. hook capture through the Claude Code adapter
payload() {
  "$PY" -c 'import json, sys; print(json.dumps({"session_id": "sess-check", "cwd": sys.argv[1], "hook_event_name": sys.argv[2], "tool_name": "Bash", "tool_input": {"command": sys.argv[3]}, "tool_use_id": "tu1"}))' "$NWORK" "$1" "\"$NPY\" check_app.py"
}
pre="$(payload PreToolUse)"
post="$(payload PostToolUse)"
echo "$pre" | tt hook claude
echo "$post" | tt hook claude
tt ledger | tail -1 | grep -q "claude-hook"
git add -A && git commit -qm "howdy"
git log -1 --format=%B | grep -q "^Tested-Tree: tests@"

# 5. exempt docs pass; skip is stamped; report reads it all back
printf 'notes\n' > NOTES.md
git add -A && git commit -qm "notes"
git log -1 --format=%B | grep -q "^Tested-Tree: pass (exempt paths only)"
printf 'def greet():\n    return "yo"\n' > app.py
git add -A && TESTEDTREE_SKIP="docs only" git commit -qm "untested yo"
tt report --format md | tee "$LOGS/report.md"
grep -q "skipped: docs only" "$LOGS/report.md"
if tt report --fail-on none > /dev/null; then echo "expected --fail-on none to fail" >&2; exit 1; fi
tt checklist --range HEAD~1 > /dev/null

# 6. uninstall leaves plain git
tt uninstall
printf 'x\n' >> NOTES.md && git add -A && git commit -qm "plain" && ! git log -1 --format=%B | grep -q Tested-Tree

cd / && rm -rf "$WORK" "$LOGS"
echo "check.sh: all green"
