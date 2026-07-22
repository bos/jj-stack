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

- `commands/land/`, `commands/sync.py`, `commands/cleanup/rebase.py`, and
  `commands/cleanup/retirement.py`;
- `models/review_state.py`, `state/store.py`, and the journal until it is deleted; and
- landed, authority, evidence, and finalization modules under `review/`.

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
  src/jj_stack/commands/cleanup/rebase.py \
  src/jj_stack/commands/cleanup/retirement.py \
  src/jj_stack/models/review_state.py \
  src/jj_stack/state/store.py \
  src/jj_stack/state/journal.py \
  src/jj_stack/review/landed.py
.venv/bin/ruff check src/jj_stack --select C901 \
  --config 'lint.mccabe.max-complexity=10' --output-format concise
```

The Ruff count covers production code under `src/jj_stack`, not tests. The multi-path `sloccount`
command is the governed recovery-surface manifest. A slice that adds or renames an authority,
evidence, finalization, or equivalent module updates that command before adding its code.

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

Tests do not assert journal events, internal read counts, helper order, or implicit global sweep
behavior. Removing a mechanism removes its tests in the same slice.
