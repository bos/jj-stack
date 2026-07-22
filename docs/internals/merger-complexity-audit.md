# Merger implementation complexity audit

Status: governing implementation constraint for the merger work. The canonical product behavior
remains in [design.md](design.md). This document records why the first implementation attempt was
stopped and the measurable limits on its replacement.

## Stop trigger

The merger plan selected the rework candidate specifically to escape the earlier implementation's
complexity spiral. The first attempt at slices 4 through 8 recreated almost the same-size system
before slice 9 had begun:

| Tree | Production SLOC | Test SLOC | Total SLOC |
| --- | ---: | ---: | ---: |
| Pinned rework base (`a45272b1`) | 20,303 | 21,341 | 41,644 |
| Canonical-design foundation (`9749a3d5`) | 20,328 | 21,349 | 41,677 |
| Archived slice-8 tip (`29c29281`) | 22,632 | 23,698 | 46,330 |
| Escaped implementation (`ebd7e36c`) | 22,658 | 23,517 | 46,175 |

These are `sloccount` Python SLOC for `src/` and `tests/`. The attempted rework added 2,329
production SLOC and 2,357 test SLOC, about 11% in each tree. It was already 155 total SLOC larger
than the implementation it was meant to replace. The archived tip remains available at local jj
bookmark `archive-complexity-spiral-2026-07-21`.

Line count is the trigger, not the full diagnosis. Inspection confirmed the same conceptual
failure:

- the old implementation's large land reconciliation and recovery packages disappeared, but a
  1,052-SLOC landed-review subsystem, a second 334-SLOC authorization engine, and larger cleanup
  retirement took their place;
- fresh review authority was loaded through two policy-bearing mechanisms;
- selected landed evidence was implemented separately in land and cleanup;
- the split identity/baseline model retained a composite compatibility mutation model, leaving two
  state-transition systems active;
- exact and rewritten retirement used parallel cleanup machinery;
- observational recovery was added without deleting `LandNote` or the operation journal;
- four more functions crossed Ruff's complexity threshold than at the pinned base; and
- deterministic integration tests repeated the same projection, prefix-convergence, retry, and
  residue guarantees across commands and layers.

Slice 5 was the exception: exact projection removed land-time refresh behavior and reduced
production code. Replacement slices must have that shape.

## Decision

The archived implementation slices 4 through 8 are rejected. Their individual fixes may be used
as evidence, but their code and test structure are not an implementation foundation.

The replacement keeps the merger plan's safety kernel and observable behavior while changing the
implementation order:

1. **Bound persistent state.** Delete `LandNote` and the operation journal while introducing
   separate canonical maps of immutable `ReviewIdentity` and `SubmittedBaseline` values.
   Mutations operate on those maps directly; no second composite mutation model or
   backwards-compatibility layer survives. Isolate each invalid map entry once at the storage
   boundary. Identity replacement remains explicit; successful `submit` replaces the immutable
   baseline value with the newly acknowledged snapshot.
2. **Unify mutation authority.** Remove land-time refresh and implement one policy-free fresh
   observation of repository, remote, identity, baseline, PR, review ref, local revision, and
   fetched trunk. Thin command policies consume that observation for exact projection, readiness,
   leases, and expected-head guards. Reload the observation before every irreversible mutation;
   one load never authorizes a later retarget, close, merge, cleanup, or retirement.
3. **Replace convergence machinery.** Keep exact-snapshot and selected rewritten-result evidence
   as separate pure classifications, but share selected classification and retirement mechanics.
   Only exact-snapshot evidence authorizes remote finalization. Selected rewritten-result evidence
   authorizes selected convergence and dependency-aware retirement only. Selected `land` and
   `sync` never scan other stacks. Explicit `sync --all` is the only exact-snapshot global scan.
   Resubmission updates existing selected reviews only: trailing WIP stays unpublished, and
   sandwich, nonlinear, or sibling topology stops before unsafe rewriting.
4. **Validate observable fixed points.** Retain one test per distinct safety risk at the narrowest
   layer, then run the reduced hostile, interruption, and sparse-cache corpus. Do not run the live
   GitHub experiment.

This order deliberately performs deletion with the feature that makes the old mechanism
unnecessary. Simplification is not a final cleanup slice.

Shared mechanics do not merge evidence policy. Exact-snapshot and selected rewritten-result
classifications remain different types, unavailable ancestry remains different from off-trunk
ancestry, and rewritten retirement retains its dependent-path check. Linear finalization
reauthorizes between retarget and close, observes terminal PR state before cleanup, and saves
retirement per identity. Duplicate identity claims invalidate every participant; closed
exact-on-trunk residue remains retryable; sibling paths retain a rewritten link until the last
selected path converges.

Successful remote finalization remains successful if later bookmark, comment, or link cleanup
fails. That residue is advisory and independently retryable. `sync --all` continues past one
malformed, unavailable, or GitHub-failing identity without widening or rolling back another
identity's mutation. Unrelated state saves preserve each opaque invalid JSON value semantically
unchanged and uninterpreted; only an explicit repair or exact-record discard may replace or remove
it.

The shared-ancestor acceptance journey is explicit: after a squash merge, `sync --all` reports
every dependent selected path without using rewritten-result evidence for mutation; the first
selected `sync` leaves the shared link in place, and the final selected path may retire it.

## Hard budgets

The pinned base is the budget baseline. The canonical-design foundation starts 25 production SLOC
above that ceiling, so the replacement must delete at least that much before completion. A limit
breach stops implementation and reopens the design; it is not waived by adding more tests or
helper layers.

- Completed production code must not exceed 20,303 `sloccount` SLOC under `src/`.
- Completed tests must not exceed 21,600 `sloccount` SLOC under `tests/`.
- Completed production plus tests must not exceed 42,000 `sloccount` SLOC.
- No individual code slice may add more than 250 production SLOC relative to its parent. Any
  positive slice must delete the mechanism it supersedes in the same change and justify the
  remaining delta. Future deletion is not complexity credit; cumulative production still may not
  exceed the pinned-base ceiling.
- The count of Ruff `C901` findings at complexity 10 must never exceed the pinned base's 21 and
  must finish at 18 or fewer. Land, sync, authority, evidence, finalization, retirement, and state
  code finish with no `C901` finding and no suppression.
- `commands/land/` must finish at or below its pinned-base 1,690 production SLOC. The
  canonical-design foundation starts at 1,700, so this is a completion target rather than an
  intermediate stop.
- No new recovery, authority, evidence, or finalization module may exceed 500 SLOC.
- A third consecutive hardening of one subsystem is an automatic design stop, matching the review
  doctrine.

Every code-slice commit records production, test, and total SLOC plus the `C901` count. Reviews
reject wrappers that merely move lines or policy to another module.

The first replacement code commit must bring production below 20,303 SLOC; every later
replacement commit stays below it. The governed recovery surface is fixed, including renamed or
future equivalents:

- `commands/land/` and `commands/sync.py`;
- `models/review_state.py` and `state/store.py`; and
- `review/landed.py`, `review/landed_evidence.py`, `review/convergence.py`, and
  `review/observation.py`.

That surface starts at 3,305 SLOC on the canonical-design foundation and may not exceed that in
any replacement commit. It must finish at or below the pinned base's 3,295 SLOC. Every governed
Python module must finish at or below 500 SLOC; the foundation's larger
`commands/land/execute.py` is another explicit deletion target.

The governing measurements are reproducible with:

```console
sloccount src
sloccount tests
sloccount src/jj_stack/commands/land
sloccount \
  src/jj_stack/commands/land \
  src/jj_stack/commands/sync.py \
  src/jj_stack/models/review_state.py \
  src/jj_stack/state/store.py \
  src/jj_stack/review/landed.py \
  src/jj_stack/review/landed_evidence.py \
  src/jj_stack/review/convergence.py \
  src/jj_stack/review/observation.py
.venv/bin/ruff check src/jj_stack --select C901 \
  --config 'lint.mccabe.max-complexity=10' --output-format concise
```

The Ruff count covers production code under `src/jj_stack`, not tests. The multi-path `sloccount`
command is the governed recovery-surface manifest. A slice that adds or renames an authority,
evidence, finalization, or equivalent module updates that command before adding its code.

CI enforces the same limits from the checked-in `complexity-budget.toml` manifest with:

```console
uv run tools/check_complexity.py
```

The gate uses SLOCCount itself rather than a similar line-count implementation. It also limits
its own size, checks each governed module, counts Ruff `C901` findings, and collects the marked
fixed-property and replacement-specific pytest items. The dedicated Linux CI job owns the
SLOCCount dependency; the normal verification matrix remains cross-platform.
On Windows, use WSL to run the gate locally or rely on the required Linux CI job.

## Replacement measurements

| Slice | Production SLOC | Test SLOC | Total SLOC | C901 | Land SLOC | Governed SLOC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| R1: bound persistent state | 19,656 | 21,120 | 40,776 | 20 | 1,563 | 3,244 |
| R2: unify mutation authority | 19,713 | 21,234 | 40,947 | 19 | 1,511 | 3,295 |
| R3: replace convergence machinery | 19,687 | 21,246 | 40,933 | 18 | 1,550 | 3,295 |
| R4: validate observable fixed points | 19,687 | 20,782 | 40,469 | 18 | 1,550 | 3,295 |
| P1: enforce cumulative budgets | 19,667 | 20,830 | 40,497 | 17 | 1,530 | 3,275 |
| P2: repair public documentation | 19,694 | 20,830 | 40,524 | 17 | 1,531 | 3,293 |
| P3: reconcile internal facts | 19,694 | 20,830 | 40,524 | 17 | 1,531 | 3,293 |
| P4: simplify internal language | 19,694 | 20,830 | 40,524 | 17 | 1,531 | 3,293 |
| P5: batch recovery reads | 19,743 | 20,964 | 40,707 | 17 | 1,531 | 3,294 |
| P6: close cross-slice audit | 19,743 | 20,928 | 40,671 | 17 | 1,531 | 3,294 |

R1 deletes 672 production SLOC and 229 test SLOC relative to the canonical-design foundation.
Every governed module is at or below 500 SLOC; the largest is `commands/land/execute.py` at 490.

R2 adds 57 production SLOC and 114 test SLOC relative to R1. The shared observation,
exact leases, and expected-head guards replace land's diff comparison, review-branch refresh,
approval-after-refresh, local-trunk rollback machinery, and redundant land-plan fields in the same
slice. Land shrinks by 52 SLOC, the governed surface meets its final 3,295-SLOC ceiling, and its
largest module is `commands/cleanup/rebase.py` at 438 SLOC.

R3 deletes 26 production SLOC and adds 12 test SLOC relative to R2. Pure exact and rewritten
evidence, selected-path convergence, dependency-aware retirement, and explicit `sync --all`
replace the two cleanup rebase/retirement modules and the implicit global land sweep. The governed
surface meets its final 3,295-SLOC ceiling, every governed module stays below 500 SLOC, and the
largest is `commands/sync.py` at 466 SLOC.

R4 leaves production unchanged and deletes 464 test SLOC relative to R3. Removing duplicate fixed
examples, dead scenario vocabulary, and overlapping deterministic front doors pays for the three
stronger child-process traces while reducing the total tree to 40,469 SLOC. All production and
governed-surface budgets remain at their R3 values.

P1 makes the existing limits executable in CI. Marking the two bounded pytest corpora and testing
the gate's environment isolation add 48 test SLOC; splitting landability classification by
pull-request lifecycle deletes 20 production SLOC and removes the governed surface's remaining
`C901` finding instead of weakening the gate to fit it. The gate itself lives under `tools/`, so
it does not inflate either historical measured tree; its separate 150-SLOC ceiling keeps that
exclusion from hiding a new metrics framework while leaving room for correctness fixes.

P2 adds 27 production SLOC and no test SLOC to make built-in help and recovery output name
concrete commands, remote effects, and safe next steps. It removes stale public warnings,
distinguishes merge and direct-push recovery, and keeps all cumulative and governed limits below
their ceilings.

P3 changes documentation only. Production, test, and governed measurements therefore remain
identical to P2.

P4 removes implementation history, repeated test inventories, and project-specific shorthand from
the active internal guides. The historical audit remains unchanged as the record of the failed
attempt and replacement. This slice changes documentation only, so all measurements remain
identical to P3.

P5 replaces per-record ancestry and PR reads with chunked `jj` and GraphQL reads. A failed GraphQL
batch falls back to at most eight concurrent REST reads while retaining a separate error for each
PR. Mutation and retirement remain sequential and freshly authorized. Reusing the same batched
ancestry classifier for selected and repository-wide recovery pays for the change without
increasing the governed recovery surface beyond its 3,295-SLOC ceiling.

P6 corrects the documented recovery boundary: `sync` reconciles merges GitHub already accepted,
but the user reruns `land --via merge` to land any remaining PRs. It also states that a dry run
cannot show the post-rebase PR-update plan. Two adapter tests added in P5 now count toward the
replacement-test budget; overlapping authority variants and a duplicate recovery front door were
consolidated to keep that budget at 30. Active guides no longer certify their own status or direct
contributors to record future slice history there; `jj` remains the implementation record.
Production is unchanged, while the test tree shrinks by 36 SLOC.

## Test budget

The replacement suite keeps convergence properties rather than interruption matrices:

- one matcher/classifier table for each pure identity or landed-evidence rule;
- one front-door integration for each projection, authority, selected-scope, sibling, sandwich,
  nonlinear-suffix, trailing-WIP, existing-review-only, and isolated-global-scan boundary;
- one user-state and one external-error journey for accepted-prefix convergence;
- three child-process retry traces around trunk push, accepted PR merge or mid-finalization, and
  per-link retirement save;
- the six neutralized harness traces, either retained directly or mapped explicitly to an
  equivalent stronger fixed-point case; and
- broader edit syntax and hostile combinations only in opt-in randomized runs.

Replacement-specific deterministic coverage is limited to 30 collected pytest items. Every
parameter expansion counts separately. The default fixed property corpus is limited to 16 cases;
broader generation remains opt-in.

R4 reduces the unconfigured property adapter from 92 cases to 16 fixed observable-risk
representatives. Broader generators remain available through the opt-in runner; focused
deterministic tests replace the removed one-off transition vocabulary. A unit guard caps the sum
of the ten family defaults at 16, and collection with two xdist workers verifies identical node
IDs.

The hand-written replacement coverage now collects exactly 30 items: two retained R1 front doors,
13 R2 authority and projection items, 11 R3 convergence and evidence items, and four R4 items
(the three process-death traces plus the fixed-corpus budget guard). R4 reached that count by
folding ten projection expansions into one classifier table, deleting four malformed-state command
duplicates, and replacing three in-process interruption cases plus one redundant trunk-resolution
front door with narrower or stronger coverage.

The previously unnamed six neutralized harness traces are now auditable:

- `push-reorder-without-resubmit-auto-resubmits-moved-prefix`: exact projection blocks until
  `submit`; the projection table and
  `test_land_requires_submit_after_diff_equivalent_rebase` cover the fixed point.
- `push-abandon-auto-resubmits-rebased-survivors`:
  `push-abandon-without-resubmit-stops-at-rebased-survivor`.
- `retry-after-trunk-push-acknowledgement-loss-converges`: child termination after the accepted
  trunk push, followed by `sync --all`.
- `retry-before-direct-land-state-commit-converges`:
  `retry-before-retirement-save-converges` and the retirement-save child termination.
- `retry-mid-finalize-converges-without-double-close`: the retained mid-finalization property and
  exactly-once event window.
- `handoff-interrupted-merge-land-recovers-through-sync`: the retained interrupted-merge handoff
  plus accepted-merge child termination.

The child-process corpus runs the CLI in a disposable process, persists the fake external service
after the accepted effect, terminates with `os._exit`, and performs recovery in a fresh process.
It covers a successful trunk push, an accepted PR merge, and the boundary before a per-link
retirement save. These checks establish local observational convergence without claiming real
power-loss durability or live-GitHub semantics.

The sparse-cache measurement extends the isolated `sync --all` front door with 64 complete
records whose well-formed submitted commits are unavailable, one malformed submitted commit ID,
absent GitHub PRs, one incomplete record, one head mismatch, one closed off-trunk PR, and one
independently recoverable exact review. It now performs one submitted-commit ancestry scan for all
68 complete records. The normal PR path needs three GraphQL requests for the 66 nonexact records.
The test forces fallback, including one malformed REST response, and the independent exact review
still retires. On the local test machine that slower path completed in 4.14 seconds wall time
(3.83 seconds inside pytest). Focused adapter tests prove the limit of eight concurrent REST
requests and 25 PRs per GraphQL request. Reported merge-result commits, if any, share one further
batched ancestry pass. This is a local performance and failure-isolation check, not a live-network
latency claim.

Focused adapter tests assert batching where call scaling is the contract. Integration tests
assert recovery outcomes, not private helper calls. Tests do not assert journal events, private
helper order, or implicit global sweep behavior. Removing a mechanism removes its tests in the
same slice.
