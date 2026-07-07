# Distributed state model

`jj-stack` coordinates four state-holders that can move independently. Every confusing
bug report and every fail-closed diagnostic is some pair of them disagreeing. This file
names the holders, the legal transitions that move each one, the invariants that define
"healthy", and the required behavior for each drift class. The property harness
([property-testing.md](property-testing.md)) generates scenarios directly from this
vocabulary.

## The four state-holders

1. **Local `jj` view** — the commit DAG, change visibility/mutability, local bookmarks,
   and remembered remote-bookmark observations. Moved by the user's `jj` commands
   (rebase, squash, abandon, new, describe), by `jj git fetch`, and by `jj-stack` itself
   (bookmark moves, pushes, `cleanup --rebase`).
2. **Remote Git refs** — the branch namespace of the GitHub repository. Moved by
   `jj-stack` pushes, by anyone else's pushes (a teammate landing to `main`, an agent
   pushing a branch with plain git), and by branch deletion from the GitHub UI or `gh`.
3. **GitHub PR database** — PRs with head/base refs, open/closed/merged state, draft
   flags, reviews, labels, comments. Moved by `jj-stack` mutations, by humans and agents
   through the UI or `gh`, and by GitHub itself: it auto-closes an open PR whose head
   becomes reachable from its base, and closes PRs whose head branch is deleted.
4. **Tracking store** — `jj-stack`'s saved beliefs: per-change bookmark name, PR
   number/URL, last-known PR state, `last_submitted_*` pointers, unlinked markers. Moved
   only by `jj-stack` commands, but it can go stale relative to everything else because
   the other three holders never notify it.

The `jj` DAG is the source of truth for stack topology; GitHub is authoritative for PR
outcomes and remote branch tips; the tracking store is a sparse cache of identity claims
("change X is reviewed by PR N via branch B") that must be re-verified against the other
holders before any mutation.

## Healthy linkage

For each submitted change, health is one chain of agreements:

- the saved bookmark exists locally and points at the change's current commit, or the
  change was rewritten and `submit` may move it
- the same-named remote ref points at the saved `last_submitted_commit_id` (or already at
  the current commit after an interrupted push)
- GitHub reports exactly one PR for that head branch, open, with the saved PR number
- the PR base is the parent change's bookmark, or the trunk branch for the bottom change

`submit` re-derives everything else from the DAG on every run, so anything not in this
chain (subjects, diffs, comment prose, base ordering) is allowed to drift freely and is
simply regenerated.

## Legal transitions worth modeling

The model deliberately covers only transitions a well-behaved user, teammate, agent, or
GitHub itself can perform through supported interfaces. It excludes catastrophic or
adversarial states (state-file corruption, repo deletion mid-command, hand-edited `.jj`
internals): the tool promises fail-closed behavior for reachable drift, not defenses
against every conceivable corruption.

| Drift kind | Boundary | Wild example | `submit` outcome |
| --- | --- | --- | --- |
| `closed_pr` | GitHub PRs | someone closes a stack PR in the UI | fail closed (exit 1) |
| `merged_pr` | GitHub PRs | a review-branch PR is merged despite policy | fail closed (1) |
| `pr_replaced` | GitHub PRs | PR closed, new PR opened on the same branch via `gh` | fail closed (1) |
| `pr_base_retargeted` | GitHub PRs | someone retargets a PR base to `main` | success; base recomputed |
| `pr_draft_toggled` | GitHub PRs | someone converts a PR to draft | success; draft preserved |
| `remote_branch_drift` | remote refs | review branch force-pushed elsewhere | fail closed (1) |
| `remote_branch_deleted` | remote refs | review branch deleted (GitHub closes its PR) | fail closed (1) |
| `trunk_advanced` | remote refs | a teammate lands unrelated work on `main` | success |
| `wrong_saved_pr_number` | tracking store | cross-machine or manual repair left a stale link | fail closed (1) |
| `unlinked_change` | tracking store | the user ran `unlink` and forgot | fail closed (1) |
| `foreign_branch_fetched` | local jj | a fetched foreign branch pins a stack commit | fail closed (2) |
| `conflicted_rebase` | local jj | rebase onto moved trunk left conflicts | fail closed (3) |
| `merge_commit` | local jj | the selection includes a merge commit | fail closed (2) |
| `agent_recreated_change` | composite | the recreated-PR incident (below) | fail closed (2) |

Two local-`jj` mechanics deserve emphasis because they are how *remote* actions corrupt
the *local* stack:

- **Fetch-induced immutability.** `jj`'s default `immutable_heads()` includes untracked
  remote bookmarks. Fetching after anyone pushes a foreign branch that points at a stack
  commit makes that commit — and its ancestors — immutable, so the stack is no longer
  reviewable.
- **Fetch-induced divergence.** If the foreign branch points at a commit that a local
  rewrite already replaced, the fetch resurrects the hidden predecessor and the change
  becomes divergent; even resolving the change ID to a single revision fails.

The composite `agent_recreated_change` scenario is the observed incident that motivated
this model: an agent closed a reviewed PR, deleted its review branch, abandoned the local
change, recreated the same work as a new change, pushed it with plain git, opened a
replacement PR with `gh`, and fetched — leaving an immutable recreated change, a second
ref on the same commit, and a tracking store still pointing at the closed PR.

## Required behavior per drift class

- **Self-healing (success class).** Drift that cannot corrupt review identity is repaired
  or ignored by the next `submit`: bases are recomputed from the DAG, trunk advances are
  irrelevant to review-branch pushes, and draft state is preserved. The full post-submit
  contract must hold afterward.
- **Fail closed (verification class).** Any drift that makes review identity unprovable
  stops `submit` before *any* mutation — no local bookmark moves, no pushes, no PR
  creates/updates — with a contractual exit code and a targeted diagnostic naming the
  repair path. Verification is ordered: stack shape and conflicts (local), then remote
  ref safety, then PR discovery and saved-link consistency, all before the mutation
  phase begins.
- **Report always (inspection class).** `view` must produce a report or a targeted
  diagnostic for every reachable drifted state — exit `0`, `2`, or `10` — never a
  traceback or an unclassified subprocess error.

Recovery stays explicit and narrow: `relink` reattaches a PR to a change, `restart` /
`submit --restart` mint fresh review identity, `unstack --cleanup --pull-request` retires
orphans, `cleanup --rebase` repairs local ancestry after merges. Drift never triggers
silent re-linking or replacement PRs.

## Why an executable model rather than TLA+/Lean

The drift vocabulary above is small and its composition rules are shallow (one or two
drifts over one optional edit), so the interesting verification is not state-space
search — it is whether the *real* `jj` binary, a faithful GitHub simulation, and the real
CLI agree with the model's verdicts. A formal spec would have to re-model `jj` rewrite
semantics, fetch-induced immutability, and GitHub's auto-close rules, and would then
verify the spec rather than the tool; the executable harness checks the same predictions
against the actual integration boundary. If the vocabulary ever grows genuinely
interaction-heavy (concurrent commands, multi-remote), a TLA+ sketch of the transition
lattice could become worthwhile for oracle-completeness checking; that idea is parked in
[backlog.md](backlog.md).
