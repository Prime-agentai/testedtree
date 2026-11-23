# Live runs: Claude Code 2.1.330 + testedtree 0.1.0

Recorded 2026-11-23 on Linux with `claude -p --model haiku --settings hooks.json`.
`hooks.json` is the output of `testedtree install claude --print`, the repo has
`testedtree install` (git hooks) and `mode = "enforce"`. The transcripts are trimmed
to tool calls and results. Nothing else was edited.

## 1. "Change the greeting, then commit it"

The agent's own verification was `python3 check_app.py; echo "exit=$?"`. Because
of the `;` its exit status says nothing about the check, so it was not counted.
The gate blocked the commit, named the fix, and the agent took it.

```text
$ git status --short && grep -n "hello" app.py; ls -la; git log --oneline -3
  2:    return "hello"
  total 88
  drwxrwxr-x   4 agentbot agentbot  4096 Nov 23 20:21 .
  drwxrwxrwt 249 root     root     61440 Nov 23 20:21 ..
  drwxrwxr-x   8 agentbot agentbot  4096 Nov 23 20:21 .git
  drwxrwxr-x   2 agentbot agentbot  4096 Nov 23 20:21 .testedtree
  -rw-------   1 agentbot agentbot   827 Nov 23 20:21 .testedtree.toml
  -rw-rw-r--   1 agentbot agentbot    48 Nov 23 20:21 app.py
  -rw-rw-r--   1 agentbot agentbot    88 Nov 23 20:21 check_app.py
  817d0d9 adopt
$ cat app.py && echo ---- && cat check_app.py && echo ---- && cat .testedtree.toml 2>/dev/null | head -50
  def greet():
      return "hello"
  
  print(greet())
  ----
  from app import greet
  import sys
  sys.exit(0 if greet() in ("hello", "hi there") else 1)
  ----
  # testedtree: a commit passes the gate only if every required check passed
  # on a tree where the committed files are byte-identical.
  # This file is read from HEAD, so edits take effect after they are committed.
  
  [gate]
  mode = "enforce"        # off | observe (warn + stamp) | enforce (block)
  require = ["tests"]
  coverage = "paths"      # paths: changed files identical | exact: whole tree identical
  escape_after = 2        # enforce: let a session through after N blocks (stamped as override)
  strikes = 3             #
[Edit] {"replace_all": false, "file_path": "/tmp/tt-e2e/app.py", "old_string": "    return \"hello\"", "new_string": 
  The file /tmp/tt-e2e/app.py has been updated successfully.
$ python3 check_app.py; echo "exit=$?"; python3 app.py
  hi there
  exit=0
  hi there
$ git add app.py && git commit -m "friendlier greeting" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>" && git log --oneline -2 && git status --short
  Exit code 1
  testedtree: COMMIT BLOCKED, the files you are committing were not tested as they are:
    tests: MISSING, no passing run recorded
    fix: run `testedtree run tests` and commit again (other runs of these checks do not count)
    (block 1 for this session)
$ testedtree run tests; echo "exit=$?"
  testedtree: tests: $ python3 check_app.py   [tree f3bec9586314]
  hi there
  testedtree: tests: PASS on tree f3bec9586314 (34 ms)
  exit=0
$ git add app.py && git commit -m "friendlier greeting" -m "Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>" && git log --oneline -2 && git status --short
  testedtree: ok, the committed files were tested as they are (tests)
  [main fc5da39] friendlier greeting
   1 file changed, 1 insertion(+), 1 deletion(-)
  fc5da39 friendlier greeting
  817d0d9 adopt
  ?? __pycache__/
```

Resulting commit:

```text
friendlier greeting

Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>
Tested-Tree: tests@f3bec9586314
```

## 2. Hook capture of the agent's own run

When the agent runs the configured command exactly (`python3 check_app.py`), the
PreToolUse/PostToolUseFailure hooks record it with no wrapper:

```text
2026-11-23T19:21:49  tests      fail exit=1       tree=5a89320b8c80* claude-hook python3 check_app.py
```

(`*` = the tree changed during the run: this fixture repo had no `.gitignore`, so
`__pycache__/` appeared. Ignore build output to keep evidence clean.)

## 3. Three strikes

After the third consecutive failure the model received, verbatim:

```text
PostToolUseFailure:Bash hook additional context: testedtree: `tests` has now failed 3 times in a row. Stop changing code to make it pass. Summarise what you tried and what you think is wrong, and ask a human how to proceed. Do not skip, weaken or delete the check.
```

In that run the count read 4, because one failure was left over from the previous
session, and the agent pointed out the mismatch. Since then strikes count per agent
session, with a regression test for it.

