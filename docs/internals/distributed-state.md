# Distributed state model

Status: current drift model. [design.md](design.md) remains the behavioral authority.

`jj-stack` coordinates four sources of state that can move independently. Drift bugs arise when
they disagree. This file names those sources, the supported transitions that move them, the
agreements required for a healthy review, and the required behavior for each kind of drift. The
property harness ([property-testing.md](property-testing.md)) generates the rows marked "property"
below; `DRIFT_KIND_SPECS` in `tests/support/submit_property_scenarios.py` is that generated
inventory. Focused command tests cover the rows marked "deterministic." Rows marked "specified"
have defined behavior but no dedicated current scenario.

## The four sources of state

1. **Local `jj` view** — the commit DAG, change visibility/mutability, local bookmarks,
   and remembered remote-bookmark observations. Moved by the user's `jj` commands
   (rebase, squash, abandon, new, describe), by `jj git fetch`, and by `jj-stack` itself
   (bookmark moves, pushes, selected `sync`).
2. **Remote Git refs** — the branch namespace of the GitHub repository. Moved by
   `jj-stack` pushes, by anyone else's pushes (a teammate landing to `main`, an agent
   pushing a branch with plain git), and by branch deletion from the GitHub UI or `gh`.
3. **GitHub PR database** — PRs with head/base refs, open/closed/merged state, draft
   flags, reviews, labels, comments. Moved by `jj-stack` mutations, by humans and agents
   through the UI or `gh`, and by GitHub itself: it auto-closes an open PR whose head
   becomes reachable from its base, and closes PRs whose head branch is deleted.
4. **Tracking store** — separate versioned `ReviewIdentity` and `SubmittedBaseline` records
   keyed by full `change_id`. Identity holds host/repository, PR number, canonical head owner/ref,
   bookmark ownership, and link state; baseline holds the exact submitted commit. Explicit
   attach, detach, restart, and repair commands change identity. A successful `submit`, or one
   that recognizes a completed push after interruption, changes the baseline. Landing, recovery,
   unstacking, or cleanup may remove both. Status observation never writes either record.

The `jj` DAG determines stack topology and content. The fetched trunk commit for the configured
remote supplies ancestry evidence for the two landed rules in [design.md](design.md); ancestry
alone does not authorize a mutation. GitHub determines PR identity, lifecycle, reviews, and
merge-result identity. Saved identity and baseline records may block a mutation when they disagree
with current state, but cannot authorize one by themselves. Every mutation rechecks the relevant
sources.

## Healthy linkage

For each submitted change, health is one chain of agreements:

- current configuration resolves to the saved host and repository
- the identity's canonical head ref is unambiguous and its owner matches the live PR
- the remote ref points at the submitted baseline, or at the exact current commit after an
  interrupted push that `submit` may safely adopt when all saved identity fields match
- GitHub reports the saved PR number on that exact head owner/ref

`submit` re-derives titles, bodies, comments, and bases from current state. Those fields do not
prove review identity. GitHub-owned draft state and reviews remain live observations.

## Legal transitions worth modeling

The model deliberately covers only transitions a well-behaved user, teammate, agent, or GitHub
itself can perform through supported interfaces. It excludes catastrophic or adversarial states
(state-file corruption, repo deletion mid-command, hand-edited `.jj` internals): the tool promises
fail-closed behavior for reachable drift, not defenses against every conceivable corruption.

Current configuration selects the remote and repository to compare. It is an input to linkage,
not a fifth independently changing state store. "Affected input(s)" names every source involved;
the generated scenario code records only the source expected to produce the primary diagnosis.

| Drift kind | Affected input(s) | `submit` outcome | Coverage |
| --- | --- | --- | --- |
| `closed_pr` | GitHub PRs | fail closed (exit 1) | property |
| `merged_pr` | GitHub PRs | fail closed (1) | property |
| `pr_replaced` | GitHub PRs | fail closed (1) | property |
| `repository_retargeted` | config, GitHub | fail closed (1) | specified |
| `head_ref_renamed` | GitHub PRs | fail closed (1) | specified |
| `remote_swapped` | config, remote refs | fail closed (1) | specified |
| `pr_base_retargeted` | GitHub PRs | success; base recomputed | property |
| `pr_draft_toggled` | GitHub PRs | success; draft preserved | property |
| `remote_branch_drift` | remote refs | fail closed (1) | property |
| `remote_branch_deleted` | remote refs, GitHub PRs | fail closed (1) | property |
| `trunk_advanced` | remote refs | success | property |
| `wrong_saved_pr_number` | tracking store | fail closed (1) | property |
| `unlinked_change` | tracking store | fail closed (1) | property |
| `foreign_branch_fetched` | remote refs, local `jj` | fail closed (2) | property |
| `conflicted_rebase` | local `jj` | fail closed (3) | deterministic |
| `merge_commit` | local `jj` | fail closed (2) | deterministic |
| `agent_recreated_change` | all four sources | fail closed (2) | property |

Two local-`jj` mechanics deserve emphasis because they are how *remote* actions corrupt
the *local* stack:

- **Fetch-induced immutability.** `jj`'s default `immutable_heads()` includes untracked
  remote bookmarks. Fetching after anyone pushes a foreign branch that points at a stack
  commit makes that commit — and its ancestors — immutable, so the stack is no longer
  reviewable.
- **Fetch-induced divergence.** If the foreign branch points at a commit that a local
  rewrite already replaced, the fetch resurrects the hidden predecessor and the change
  becomes divergent; even resolving the change ID to a single revision fails.

The fixed composite `agent_recreated_change` scenario closes a reviewed PR, deletes its review
branch, abandons and recreates the local work, pushes it outside the tool, opens a replacement PR,
and fetches. The result is an immutable recreated change, another ref on the same commit, and
saved tracking that still points at the closed PR.

## Required behavior per drift class

- **Repairable drift.** Drift that cannot corrupt review identity is repaired or ignored by the
  next `submit`: bases are recomputed from the DAG, trunk advances are irrelevant to review-branch
  pushes, and draft state is preserved. The full post-submit contract must hold afterward.
- **Fail closed.** Any drift that makes review identity unprovable
  stops `submit` before *any* mutation — no local bookmark moves, no pushes, no PR
  creates/updates — with a contractual exit code and a targeted diagnostic naming the
  repair path. Verification is ordered: stack shape and conflicts (local), then remote
  ref safety, then PR discovery and saved-link consistency, all before the mutation
  phase begins. The diagnostic carries a structured condition (`DriftError.condition` for
  remote-ref, PR, and tracking-store checks; `UnsupportedStackError.reason` and
  `ConflictedStackError` for local shape), so the harness asserts *which* check fired,
  not just the exit code — a stop for the wrong reason names the wrong repair path and
  must fail the model.
- **Recheck before mutation.** Planning observations never authorize a later mutation. Reload the
  configured repository, live PR identity/head/readiness, and relevant refs immediately before
  each irreversible action; use an exact lease or expected-head guard.
- **Inspection must still report.** `view` must produce a report or a targeted
  diagnostic for every reachable drifted state — exit `0`, `2`, or `10` — never a
  traceback or an unclassified subprocess error.

Recovery stays explicit and narrow: `relink` reattaches a PR to a change; `restart` and
`submit --restart` create new review identity; and `unstack --cleanup --pull-request` closes and
cleans up each orphan it can verify. Selected `sync` rebases one selected stack after proving that
ancestors landed. After fresh identity and head checks, `sync --all` may retarget and close landed
PRs whose exact submitted commits are on trunk and remove tracking when no visible stack still
needs it. Drift never triggers silent relinking or replacement PRs.

## Why an executable model rather than TLA+/Lean

The valuable check is whether the real `jj` binary, fake GitHub server, and CLI agree on the same
result. A separate formal model would need to reproduce `jj` rewrites, fetch-induced immutability,
and GitHub auto-close behavior without replaying the implementation boundary. The current
executable harness checks those predictions directly. Possible extensions for concurrent commands
or multiple remotes belong in [backlog.md](backlog.md).
