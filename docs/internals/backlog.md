# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Recovery

_Benefit: medium — affects users with interrupted operations, which is uncommon
but leaves them stuck with inconsistent state until resolved._

Intent files now act as the concurrency lock, mutating commands hard-fail when
saved jj-review data is unavailable, saved-data writes are incremental during
mutating operations, `status` surfaces outstanding and stale incomplete
operations, and `abort` retracts completed work from an interrupted submit and
removes the intent file.

The remaining follow-up in this area is extending abort to cover partial land
retraction and `close` reversal (reopening closed PRs), both of which require
GitHub access and careful ordering of retraction steps.

## Ancestor Merged on GitHub

_Benefit: small — remaining edge cases are narrow and infrequent._

The design doc and future `land` design now cover the main recovery shape for
merged ancestors and the division of labor between `land` and
`cleanup --rebase`.

The remaining follow-up here is narrower:

- edge cases around partial-stack landing boundaries after some earlier changes
  have already landed
- whether future landing transports impose extra constraints on how descendants
  are rediscovered and resubmitted
- any residual diagnostics that are still too subtle once the concrete `land`
  flow exists

## Repo-Scoped Sync

_Benefit: medium — useful for operators managing several stacks at once, but
not blocking the core single-stack workflow._

A future `import` design covers explicit stack materialization for one
selected review stack, and `status --fetch` remains the read-only refresh
primitive.

The remaining open question is whether the product should also grow a
repo-scoped `sync` command that:

- refreshes remote review observations across more than one selected stack
- decides when local bookmark materialization should happen automatically
- coordinates with `cleanup --rebase` without turning refresh into implicit
  history repair

## Landing Transports and Merge Queues

_Benefit: medium — high value for teams that require merge queues, but complex
to design correctly and not blocking the current direct-push flow._

The current `land` model is intentionally narrow: resolve the ready prefix,
move local history first, then reconcile GitHub state around that result.

The remaining product question is whether landing should eventually support
more than one transport while keeping the `jj` DAG as the source of truth.
Concrete follow-up questions:

- whether `land` should grow an explicit transport selector such as direct
  push to trunk, open a landing PR, or submit the ready prefix to a merge
  queue
- how queue-backed landing should report queued, running, failed, and merged
  states in `status` without introducing forge-owned stack metadata as a
  competing source of truth
- how the queue or landing-PR path should preserve the current fail-closed
  behavior when the ready prefix changes locally while a queued landing is in
  flight
- whether queue-backed landing needs resumable intent state distinct from the
  current direct-landing intent model
- how repo policy requirements such as required checks, branch protection, and
  review-only `review/*` branches should be diagnosed before a landing attempt

This should be designed explicitly rather than bolted onto the current `land`
flow piecemeal.

## Guided Recovery and Next-Step UX

_Benefit: large — daily operator quality of life; makes the safe next action
obvious without requiring users to read internal design notes._

The command surface is intentionally small, but the operator experience still
depends heavily on knowing what to run next after a non-trivial state change.

Useful follow-up work here includes:

- richer "next command" guidance after `submit`, `land`, `close`, and
  `cleanup --rebase`
- clearer distinction between "inspect only", "safe retry", and "history
  rewrite" recovery paths when something is stale or ambiguous
- an explicit guided-recovery flow for common cases such as "ancestor already
  landed", "remote branch disappeared", or "saved state no longer matches the
  selected stack"
- whether some of the current recovery-oriented guidance should eventually live
  behind a dedicated helper command rather than being repeated ad hoc in
  diagnostics

This is partly presentation, but it is also a real product capability: the
tool should make the safe next action obvious without requiring the operator to
read internal design notes.

## Pre-Push Auto-Close Predictor — Out-of-Stack Base Coverage

_Benefit: small — protects an unusual case (a PR base that already contains
the planned new head, while sitting outside the submitted stack), but the
case is rare in practice._

The pre-push auto-close predictor in `submit` covers both the common stacked
reorder case and the anomalous case where a non-stack base already contains
the new head. The integration coverage today exercises only the stacked
shape: a reorder fixture where every base sits inside the push set.

The remaining follow-up here is a focused integration test that constructs
the out-of-stack shape — for example, a PR whose base is the trunk branch
after the change has been merged into trunk by some other route — and shows
that the predictor pre-retargets it before push. The fake GitHub already
simulates the head-contained-in-base auto-close, so the missing piece is the
fixture, not the simulator.

## Submit Bookmark Same-Change Fallback Path Coverage

_Benefit: small — a safety-net branch with no direct test today, easy to
let rot._

The bookmark-managed check that decides whether to pass `allow_backwards`
to `set_bookmark` has two arms: a fast path off the saved cached state's
`manages_bookmark` record, and a fallback that asks `jj` whether the
bookmark's current local target resolves to the same `change_id` as the
desired commit. The split integration test exercises only the first arm
because the prior submit always populates the cache. A focused fixture
that wipes or constructs a state without the managed record (e.g., an
imported or relinked stack on its first submit, or a state file mutated
between submits) would lock in the fallback so a future refactor cannot
silently break it.

## Submit + `jj squash` Coverage

_Benefit: small — squash is a common edit; today there is no integration
test that exercises it specifically._

`jj squash` rewrites two changes into one (or moves content from one into
another and abandons the source). The existing abandon test deletes a
change but does not exercise the squash-with-content-moved pattern, which
interacts with the PR auto-close predictor and the bookmark-tracking rules
differently — the survivor's commit grows to include the abandoned change's
diff, while the abandoned change's PR becomes orphaned. Worth a focused
fixture so the squash shape is locked in independently of the existing
abandon coverage.

## Post-Submit Closure Detector — Coverage Gaps

_Benefit: small — the predictor and the existing detector already cover the
loud failure modes; these are residual gaps where state changes are silent
or extremely rare._

The post-submit detector raises when a PR transitions open → closed during
`submit`. It does not currently distinguish:

- a PR whose `is_draft` flipped during the run (state stays `"open"` either
  way) — fine for the auto-close case but would not surface a hostile draft
  toggle initiated outside `submit`
- a PR that GitHub closed and a third party reopened mid-run; the detector
  reads the post-run state and considers it clean
- a PR that disappeared entirely (deleted, transferred) between discovery
  and refetch; the helper currently silently skips a missing entry rather
  than surfacing it

If any of these turn out to bite real users, broaden the detector to compare
more fields and to treat missing PRs as anomalies rather than absences.

## Documentation

_Benefit: large — Phases 2–4 increase adoption and reduce confusion;
without complete task-oriented guides, all other features are underutilized._

Phase 1 is complete: the README has a quickstart, and `docs/` has
`daily-workflow.md`, `mental-model.md`, and `troubleshooting.md`. Internal
design and implementation notes live under `docs/internals/`.

Remaining work:

- **Phase 2 (partial):** `mental-model.md` exists, but there is no standalone
  landing/cleanup guide, no importing-existing-PRs guide, and no cheatsheet
  for operators who already know the model.
- **Phase 3:** generated or semi-generated command reference pages that stay
  in sync with the argparse surface; doc drift checks that fail CI when
  committed reference pages diverge from actual `--help` output; example
  transcripts captured from the fake GitHub test environment.
- **Phase 4:** LLM-friendly exports (`llms.txt` / `llms-full.txt`) once the
  primary docs structure is stable.

Docs should teach the workflow first and enumerate commands second. The primary
risk is writing reference prose before the task-oriented guides are complete.
