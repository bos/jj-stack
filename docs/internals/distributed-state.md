# Distributed state model

Status: current drift model. [design.md](design.md) defines product behavior.

`jj-stack` coordinates four sources of state that can move independently. Drift bugs arise when
they disagree. This file names those sources, the supported transitions that move them, the
agreements required for a healthy review, and the required behavior for each kind of drift. The
property harness ([property-testing.md](property-testing.md)) generates the rows marked "property"
below; `DRIFT_KIND_SPECS` in `tests/support/submit_property_scenarios.py` is that generated
inventory. Focused command tests cover the rows marked "deterministic." Rows marked "specified"
have defined behavior but no dedicated current scenario.

## The four sources of state

1. **Local `jj` view** — the commit DAG, change visibility/mutability, ordinary local bookmarks,
   and fetched non-review remote observations. Moved by the user's `jj` commands (rebase, squash,
   abandon, new, describe), by ordinary fetch, and by `sync`. Managed review branches
   are deliberately absent from this durable local view.
2. **Remote Git refs** — the branch namespace of the GitHub repository. Moved by
   `jj-stack` atomic leased pushes, by anyone else's pushes (a teammate merging to `main`, an
   agent pushing a branch with plain git), and by branch deletion from the GitHub UI or `gh`.
3. **GitHub review state** — PRs with head/base refs, open/closed/merged state, draft
   flags, reviews, labels, comments, plus GitHub stack resources with ordered membership
   and historical merge results. Moved by `jj-stack` mutations, by humans and agents
   through the UI or `gh`, and by GitHub itself: it auto-closes an open PR whose head
   becomes reachable from its base, closes PRs whose head branch is deleted, and records
   GitHub stack transitions.
4. **Tracking store** — the `ReviewIdentity` and `SubmittedBaseline` records described in
   [design.md](design.md). Moved by the commands that design.md allows to change an identity or
   advance a baseline; status observation never writes any of them.

What each source determines, and what a healthy link between them requires, are specified in
[design.md](design.md) — this file does not restate them. All four move independently, so any
pair can disagree, and every mutation rechecks the sources it depends on rather than trusting an
earlier observation.

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
| `foreign_branch_fetched` | remote refs, local `jj` | fail closed (2) | property |
| `conflicted_rebase` | local `jj` | fail closed (3) | deterministic |
| `merge_commit` | local `jj` | fail closed (2) | deterministic |

Two local-`jj` mechanics deserve emphasis because they are how *remote* actions corrupt
the *local* stack:

- **Fetch-induced immutability.** `jj`'s default `immutable_heads()` includes untracked remote
  bookmarks. Managed review branches are excluded from ordinary fetch, but another foreign
  branch that points at a stack commit can still make that commit — and its ancestors —
  immutable, so the stack is no longer reviewable.
- **Fetch-induced divergence.** If the foreign branch points at a commit that a local
  rewrite already replaced, the fetch resurrects the hidden predecessor and the change
  becomes divergent; even resolving the change ID to a single revision fails.

## Required behavior per drift class

- **Repairable drift.** Drift that cannot corrupt review identity is repaired or ignored by the
  next `submit`: bases are recomputed from the DAG, trunk advances are irrelevant to review-branch
  pushes, and draft state is preserved. The full post-submit contract must hold afterward.
- **Fail closed.** Any drift that makes review identity unprovable stops `submit` before *any*
  mutation — no local DAG changes, pushes, PR creates/updates, or tracking writes — with a
  contractual exit code and a targeted diagnostic naming the repair path. Verification is
  ordered: stack shape and conflicts (local), then remote ref safety, then PR discovery and
  saved-link consistency, all before the mutation phase begins. The diagnostic carries a
  structured condition (`DriftError.condition` for
  remote-ref, PR, and tracking-store checks; `UnsupportedStackError.reason` and
  `ConflictedStackError` for local shape), so the harness asserts *which* check fired,
  not just the exit code — a stop for the wrong reason names the wrong repair path and
  must fail the model.
- **Recheck before mutation.** Planning observations never permit a later mutation, per safety
  rule 4, *merge what was reviewed*, in [design.md](design.md).
- **Inspection must still report.** `view` must produce a report or a targeted
  diagnostic for every reachable drifted state — exit `0`, `2`, or `10` — never a
  traceback or an unclassified subprocess error.

Recovery is explicit and narrow, and drift never triggers silent relinking or replacement PRs.
The commands that can reattach or retire review identity — `checkout`, `relink`,
`unstack`, `sync`, and `sync --all` — are specified in
[design.md](design.md).

## Why an executable model rather than TLA+/Lean

The valuable check is whether the real `jj` binary, fake GitHub server, and CLI agree on the same
result. A separate formal model would need to reproduce `jj` rewrites, fetch-induced immutability,
and GitHub auto-close behavior without replaying the implementation boundary. The current
executable harness checks those predictions directly. Possible extensions for concurrent commands
or multiple remotes belong in [backlog.md](backlog.md).
