# Restructuring outcome

This note records the completed internal restructuring that made review state
classification, recovery records, and command phase boundaries explicit. The detailed
current architecture belongs in [implementation-strategy.md](./implementation-strategy.md);
deferred design debt belongs in [backlog.md](./backlog.md).

[design.md](./design.md) remains the product spec. If a future cleanup changes
user-visible behavior, update `design.md` in the same change before treating this
implementation note as authority.

## What Changed

The old implementation repeated the same questions in several commands:

- is this change locally present, divergent, orphaned, or missing?
- is it actively tracked, untracked, or intentionally unlinked?
- does the review branch exist, point at the expected commit, or have conflicts?
- is the pull request open, closed, merged, missing, ambiguous, or unavailable?
- does saved submitted state still agree with the live `jj` DAG?

That repeated branching has been replaced by `review/change_status.py`. The
`ReviewChangeStatus` classifier is observational only: it consumes state already loaded
by callers and names the local, link, remote-branch, pull-request, review-decision,
baseline, and saved-identity axes. Commands still own their policy and mutations.

The old intent-file system has been replaced by:

- a repo-scoped operation lock for mutating commands
- retained append-only operation journals in `state/journal.py`
- per-command recovery records folded from journal events
- `abort`, `status`, `doctor`, and mutators reading the same journal-backed operation
  list instead of per-kind intent files

Command orchestration now keeps shared dependencies in `CommandContext`, command-line
normalization in `<Command>Options`, selected work in prepared/resolved target values,
and live mutation state in run objects such as `SubmitMutationRun` and `LandMutationRun`.

## Preserved Rules

The restructuring did not change the core product model:

- the `jj` DAG remains the source of truth for stack topology
- saved review state remains a sparse cache keyed by `change_id`
- GitHub pull requests are derived from the selected local stack
- ambiguous linkage fails closed
- identity follows `change_id`, not commit identity

## Current Cleanup Bar

The migration is now in consolidation, not expansion. Follow-up work should remove code
unless it is fixing a concrete correctness issue. Good cleanup targets are:

- command-local predicates now covered by `ReviewChangeStatus`
- wrappers that only forward `CommandContext`, options, prepared targets, or run objects
- temporary documentation that narrated the migration rather than describing the current
  implementation
- stale tests whose only assertion is that retired intent-era shapes existed

Avoid deleting explicit adapter boundaries, CLI parsing, user-output rendering, GitHub
payload handling, or journal recovery cases merely to reduce line count. Those areas are
large because they encode external behavior.
