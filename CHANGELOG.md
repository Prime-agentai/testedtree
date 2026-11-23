# Changelog

## 0.1.0 (2026-11-23)

First release.

- `run`: runs the configured checks and records the result against the git tree id it ran on (`mutates = true` checks are recorded against the tree they leave behind).
- `pre-commit` gate: checks the exact staged tree, with path or exact coverage, in `off`, `observe` or `enforce` mode. A blocked session can get through after N blocks, recorded as an override. `TESTEDTREE_SKIP` lets humans skip the gate. Paths listed in `exempt` are not checked.
- `commit-msg` stamp: every commit gets a `Tested-Tree:` trailer, `none (...)` included. An empty message is left alone, so an aborted commit stays aborted.
- Diff-triggered checklist read from HEAD or a base ref. Items match path globs plus regexes on added or removed lines.
- Claude Code adapter (`hook claude`): picks up the agent's exact check commands through PreToolUse, PostToolUse and PostToolUseFailure. After three failures in a session it tells the agent to stop. It also passes the session id to the git hook, so escapes are counted per session. Verified live on Claude Code 2.1.330.
- `report`: tested, none, override, skipped and unstamped commits, split agent vs human. Lists policy changes and checklist hits, so stale items can be pruned. Outputs markdown or JSON, and `--fail-on` makes it usable as a CI gate.
- Composite GitHub Action: writes the PR report and the checklist to the job summary.
- Zero dependencies. Python 3.11+.
