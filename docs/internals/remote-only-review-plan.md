# Remote-only review branches

This temporary plan is the acceptance contract for replacing persistent local review bookmarks.
It is an explicit exception to the usual rule that `docs/internals/` does not contain task lists.
The file must shrink as the work proceeds: the commit that completes a numbered step deletes that
step from this file. The final documentation commit deletes the file itself.

No implementation begins until the coordinator and an independent reviewer approve this plan.
Every implementation commit receives an independent code review and a coordinator review before
the next step begins.

## Outcome

GitHub review branches exist only on the selected Git remote. A tracked review's stable remote
branch name is stored in its `ReviewIdentity`; jj-stack does not create a persistent local or
remote-tracking bookmark for it. Submission observes the remote immediately before mutation and
pushes commit IDs from jj's backing Git store directly to the remote URL with exact leases.

The reserved namespace remains `review/`. New branches keep the existing human-readable form
`review/<subject-slug>-<short-change-id>`; the redesign removes them from local command output
rather than making their remote names opaque. A normal fetch excludes that namespace, preventing
review branches from being imported as untracked remote bookmarks and making commits immutable.
Commands that intentionally bring a reviewed commit into a workspace import it through the fixed
temporary local Git ref `refs/heads/jj-stack-tmp/checkout` and forget that ref before returning.
The `review/` namespace is exclusively reserved for jj-stack on a selected review remote; unknown
refs in it are ignored and never claimed or deleted.

The redesign deliberately removes behavior that would otherwise require another durable state
machine:

- review branch prefixes and bookmark use are not configurable;
- user-owned bookmarks and PR branches outside the managed naming grammar cannot be adopted;
- bookmark-derived implicit review discovery and unlink markers do not exist;
- standalone `jj-stack restart` is removed;
- `jj-stack submit --restart` derives a deterministic replacement branch from the saved head ref
  and old pull-request number, without consulting a possibly edited subject;
- interrupted first submission has one narrow observational recovery rule: an exact full-change-ID
  match among remote branches ending in the change's short ID, with no GitHub pull request;
- other recovery uses the jj DAG, saved identity, GitHub, and current remote refs, never
  transaction or replay records.

## Non-negotiable constraints

- The jj DAG remains the source of truth. Tracking stays sparse and contains no pending operation,
  transaction journal, replay record, temporary-ref registry, or fetch-configuration marker.
- `ReviewIdentity.head_ref` is the sole durable authority for a tracked review branch.
- `ReviewIdentity` always names an actual PR; jj-stack never saves a branch-only, pre-PR identity.
- Every tracked `ReviewIdentity.head_ref` matches the reserved managed naming grammar under
  `review/`; checkout and relink do not adopt arbitrary same-repository branches.
- The implementation has one push path: a direct, atomic Git push to the resolved remote URL with
  an explicit `--force-with-lease=<ref>:<expected>` for every branch.
- Remote branch observation uses direct Git remote queries. Local or remote bookmark state never
  authorizes review mutation.
- The normal fetch-isolation authority is one backing-Git negative refspec,
  `^refs/heads/review/*`. Every command-owned ordinary fetch, explicit attachment, and remote
  review-ref mutation re-observes and idempotently ensures it. jj-stack does not save a configured
  marker or add a redundant `remotes.<remote>.fetch-bookmarks` setting.
- If an effective `remotes.<remote>.fetch-bookmarks` value overrides Git refspecs, jj-stack
  rejects it before mutation with origin-aware, runnable repo-level unset guidance. jj-stack does
  not parse, normalize, or compose the user's pattern expression. Dry-run reports the required
  configuration change without applying it.
- The minimum supported jj version becomes 0.43.0, the first tested release that accepts the
  required negative fetch refspec. There is no fallback for earlier jj versions.
- Direct pushes use the selected remote's resolved push URL, never its remote name, so Git does
  not create a local remote-tracking ref as a side effect.
- Explicit checkout uses `refs/heads/jj-stack-tmp/checkout`, protected by the repository operation
  lock. It imports and verifies the exact object ID, then uses `try/finally` to forget and export
  it on every handled exit. It never uses `jj git fetch --branch`, which bypasses ordinary fetch
  isolation.
- A retry removes or verifies only that exact temporary checkout artifact after an abrupt
  interruption. `doctor` reports it if present; there is no registry or generalized temporary-ref
  cleanup.
- Commands fail closed with recovery guidance when managed bookmarks were already imported; they
  do not silently migrate bookmark state. `doctor` reports imported managed bookmarks, a missing
  negative refspec, and an overriding jj fetch setting.
- Replacement code deletes the superseded bookmark mechanism in the same slice. There is no
  compatibility layer, migration path, alternate bookmark transport, or dual policy model.
- The tracking schema is bumped incompatibly when its obsolete fields are removed, with no
  migration or legacy shim.
- Dependent mutations remain ordered and re-read their authorization inputs immediately before
  changing the remote.
- Complexity budgets do not increase. If a slice needs a budget increase, a second durable branch
  representation, or a third hardening patch, work stops for a design review.
- An intermediate commit may add code when a coherent replacement needs a foundation already
  listed in this reviewed plan. Any newly discovered foundation requires a plan amendment and
  another plan review. Only the completed series is required to be a net deletion from baseline.

## Measurable success criteria

The baseline is commit `d459997adc6c18700d2cba245f2c654dfad8ec76`, measured with
`uv run tools/check_complexity.py`: 20,927 production lines, 39,890 total production-plus-test
lines, 3,289 governed landing/recovery lines, and 17 Ruff C901 findings. The baseline
`./check.py` run passes 510 tests.

The completed series must satisfy all of these:

- **Better UX:** normal `jj status` and `jj bookmark list` output contains no jj-stack-created
  bookmark before or after submit, resubmitting an existing review, checkout, relink,
  `submit --restart`, cleanup, sync, or unstack. Stable remote branch names retain the readable
  subject slug and short change ID used today.
- **Smaller codebase:** production code is at least 500 lines below baseline and total
  production-plus-test code is below baseline. Governed landing/recovery lines and C901 findings
  do not increase.
- **Simpler behavior:** there is one durable branch field, one Git-ref observation path, one push
  path, and no bookmark ownership, link-state, or unproved branch-discovery policy.
- **Safer fetches:** ordinary fetch is proven not to import managed review branches in colocated
  and non-colocated repositories. Explicit checkout retains the requested commits while leaving
  no review or temporary bookmarks.
- **Clearer docs:** public and internal docs use “branch” for the GitHub-facing name, explain
  the remote-only model once at the appropriate level, contain no obsolete bookmark workflow,
  and are shorter or more direct wherever the old mechanism is removed.
- **Preserved product behavior:** submit, resubmission, checkout, relink, cleanup, sync, unstack,
  and `submit --restart` retain their intended review workflows, with ambiguous linkage and stale
  remote state still failing closed.
- **Validated series:** every code commit passes focused checks, `./check.py`,
  `uv run tools/check_complexity.py`, and independent review.

## 1. Remove optional bookmark policy and staging commands

Create a deletion-first commit while retaining the single existing local-bookmark transport until
its replacement is complete:

- reserve `review/` exclusively for jj-stack and remove `bookmark_prefix`, `use_bookmarks`,
  `cleanup_user_bookmarks`, and `--use-bookmarks`;
- remove adoption of user bookmarks, the saved bookmark-ownership field, and ownership-specific
  cleanup policy; relink accepts only the managed branch grammar, and cleanup eligibility requires
  the namespace plus exact saved PR, head, and baseline authorization;
- remove link state and the `unlink` command; `unstack --local` remains the explicit way to remove
  local tracking;
- remove standalone `restart`;
- make `submit --restart` derive its readable fresh name only from the saved head ref, old PR
  number, and change ID, replacing rather than accumulating an earlier `fresh-pr<number>` marker;
  retain the old identity until the replacement PR and baseline are installed together;
- retain `BookmarkState`, bookmark conflicts, local bookmark movement, implicit discovery, and
  public `bookmark` vocabulary explicitly until the replacement commit;
- bump the incompatible tracking schema without a migration or shim;
- update the canonical design, affected user docs, CLI help, and focused tests in the same commit.

Acceptance: the removed configuration, state, and commands have no remaining code or
documentation path. Existing submission still has exactly one local-bookmark transport. Focused
checks and `./check.py` pass, no complexity budget grows, and review records cumulative line-count
deltas.

## 2. Address the backing Git store directly

Create a narrow foundation commit that makes existing direct Git operations correct in colocated
and non-colocated repositories:

- resolve the backing repository with `jj git root`;
- run existing remote-list, update, and delete operations against that Git directory and the
  resolved fetch or push URL, never a named remote;
- cover both repository layouts at the process boundary;
- do not add an unused review-update API or a second push path.

Acceptance: current direct Git behavior passes in both layouts, focused checks pass, no complexity
budget grows, `./check.py` passes, and review records cumulative line-count deltas.

## 3. Replace persistent bookmarks with remote-only transport

Create one coherent replacement commit:

- retain deterministic `review/<subject-slug>-<short-change-id>` branches for first submit and
  the existing readable `fresh-pr<number>` form for retry-stable `submit --restart` branches;
- raise the minimum jj version to 0.43.0;
- before changing fetch configuration, report that `review/` is reserved; inspect only exact refs
  selected by the command, never scan the namespace to classify ownership;
- observe and ensure the single negative Git fetch refspec before every command-owned ordinary
  fetch, explicit attachment, or remote review-ref mutation, including deletion; reject an
  effective jj fetch-pattern override and honor dry-run as described above;
- observe managed refs directly on the remote and push all selected revisions atomically by
  resolved URL with exact per-ref leases;
- recover an interrupted first submit only by querying `review/*-<short-change-id>`, requiring one
  candidate whose target resolves to the exact full local change ID, and proving GitHub has no PR
  for it; inspect the Git commit's `change-id` header without importing that snapshot into jj, use
  its observed target as the lease, and fail closed on multiple or unproved candidates;
- require explicit checkout or relink when a PR exists without saved identity;
- authorize checkout and relink only from the exact PR owner, head ref, and head SHA; import the
  exact selected ref through `refs/heads/jj-stack-tmp/checkout`, compare it with the observed
  remote object ID, verify it after `jj git import`, and forget/export it in `try/finally`; cover
  an interrupted-checkout retry;
- rewrite submit, resubmission, checkout, relink, cleanup, sync, unstack, and recovery around
  saved identity and direct remote observation;
- remove local bookmark mutation, tracking, repair, cleanup, conflict handling, speculative
  discovery, link handling, and the superseded bookmark models and tests, except for the exact
  interrupted-first-submit observation above;
- follow `docs/internals/property-testing.md` for all property-harness changes;
- require checkout and relink PR heads to match the reserved managed naming grammar;
- derive cleanup eligibility from that namespace plus exact saved PR, head, and baseline checks,
  and never delete an unclaimed ref;
- preserve pre-push head/base reachability simulation, protective trunk retargets, and the final
  unexpected-closure check that prevent GitHub from auto-closing a PR;
- re-read all selected remote refs in one fresh batch immediately before the atomic leased push;
  a stale lease or unsupported atomic push fails without a sequential fallback;
- delete remote review refs through the same transport with exact leases and no follow-up fetch;
- rename public JSON and output fields from `bookmark` to optional `branch`;
- update the canonical design, affected user docs, CLI help, architecture notes, and focused tests
  in the same commit.

Acceptance: successful workflows leave no jj-stack bookmark or temporary ref; ordinary fetch
isolation, exact-lease atomicity, interrupted first submit, and interrupted checkout are covered
in both repository layouts where relevant. GitHub auto-close defenses retain focused coverage.
Focused tests and `./check.py` pass, no complexity budget grows, production code is at least 500
lines below baseline, and review records cumulative line-count deltas.

## 4. Consolidate the surviving test suite

Review the surviving tests against the testing philosophy after the mechanism is gone:

- delete assertions whose only distinct risk was local bookmark implementation detail;
- retain boundary coverage for atomic leases, retries, remote drift, PR/head mismatch, cleanup
  authorization, state-loss recovery, cross-stack rewrites, and foreign fetched branches;
- reduce property scenarios to durable DAG, remote-ref, GitHub, and tracking invariants;
- add no new behavior or production mechanism in this commit.

Acceptance: the focused and full suites pass, test and total line counts fall, no complexity
budget grows, `./check.py` passes, and independent review finds no lost distinct risk.

## 5. Review and simplify all documentation

Perform a fresh end-to-end review of user-facing and internal documentation after the code
settles:

- compare every command's help and examples with actual behavior;
- make terminology and recovery guidance consistent across `README.md`, every user guide and
  schema under `docs/`, every active internal document under `docs/internals/`, command help, and
  the bundled skill;
- remove duplicate explanations, obsolete local-bookmark guidance, and implementation history;
- ensure the public explanation starts with the uncluttered jj workflow and introduces remote
  branches only where users need them;
- search for `bookmark_prefix`, `use_bookmarks`, `cleanup_user_bookmarks`, `jj-stack restart`,
  `jj-stack unlink`, `bookmark_ownership`, `link_state`, and stale local-review-bookmark
  instructions; run rendered/help checks, then obtain independent documentation and coordinator
  reviews.

Acceptance: reviewers find the public workflow coherent and the internal model easier to follow;
the measurable criteria above hold; the final full test and complexity passes succeed. Delete this
plan file in the documentation commit so no completed task list remains.
