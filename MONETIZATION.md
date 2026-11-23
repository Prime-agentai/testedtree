# Monetization thesis (viability 7/10)

**The pain is organisational.** The Harvard/Jellyfish working paper (Chen & Stratton, Oct 2026: 718 firms, ~726k workers) found that coding agents raised code volume by about 30%, while PR-to-merge time rose 49% and review comments per PR rose 35%. Firms answered with "senior approval for AI-generated code", which slowed things further. Reviewers can't see which commits were tested as they were committed. testedtree makes that a queryable fact on every commit.

## Open-core boundary

| Open source (MIT, this repo) | Paid (hosted / enterprise) |
|---|---|
| Local ledger, gate, stamp, checklist, three strikes, report, GitHub Action | **Org dashboard:** tested-as-committed rate per repo/team/agent over time, override and skip trends, never-fired checklist items across repos |
| Claude Code adapter + git hooks for any agent | **Signed evidence:** CI runners sign ledger entries (Sigstore/SSH), so the stamp becomes an attestation; verify-on-merge |
| `report --format json` | **Compliance export:** change-management evidence for SOC 2 CC8.1 / ISO 27001 A.8.32. "Every production change was tested as deployed; here are the 3 overrides and who approved them." |
| Single-repo config | **Central policy:** org-wide required checks and checklists pushed to repos; base-branch policy enforcement in the merge queue |

## Why someone pays

- Platform and DevEx teams rolling out agents need a number for "are agent commits verified?" to unlock wider adoption. Today they ask for screenshots.
- Auditors already ask for change-management evidence. Agent-authored code makes that question sharper, and a per-commit trailer plus a signed ledger is a cheap answer.

## Pricing sketch (unverified, no customers yet)

Free for open source. Team: per active committer per month, hosted dashboard. Enterprise: signed evidence, SSO, compliance export, self-hosted.

## Risks

- GitHub, GitLab or Anthropic ship an equivalent natively. Claude Code already has internal versions of these practices.
- Adoption depends on teams tolerating a commit gate. `observe` mode is the on-ramp.
