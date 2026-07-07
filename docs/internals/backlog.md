# Backlog

Items that need to be implemented or thought through, but are not blocking
current slices.

## Crash and Interrupt Diagnosis

_Benefit: medium — affects users with failed mutating commands, which is uncommon
but can leave jj, GitHub, and saved tracking data out of sync._

Mutating commands now use a repo-scoped operation lock and append events to the
repo-level operation log. Retry behavior derives from the jj DAG, saved tracking
data, GitHub state, explicit user selectors, and narrow log evidence when `land`
must prove that an unfinished run already pushed trunk. The log remains audit
evidence, not a retained recovery model.

Possible follow-up work:

- add a small diagnostic command that prints recent operation-log entries in a
  user-facing format
- document how to locate the repo state directory when debugging with support

## Start-Fresh Review Repair

_Benefit: medium — important when a previous jj-stack bug, manual GitHub
operation, or branch cleanup leaves local changes attached to closed or unusable
PRs._

`view` can now preserve and show remembered PR identity even when the saved
review branch no longer has a matching PR, and `restart` gives users an explicit
local repair command that clears stale PR identity, avoids reusing the old review
branches, and leaves the next `submit` to create fresh PRs.
The normal user-facing flow is `submit --restart`, which computes that reset in
memory and persists only the replacement PR identity after submit succeeds.

Possible follow-up work:

- consider whether status advisories should suggest a narrower per-change restart when
  only one change in a selected stack has stale PR tracking

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

A future `checkout` design covers explicit stack materialization for one
selected stack, and `view --fetch` remains the read-only refresh
primitive.

The remaining open question is whether the product should also grow a
repo-scoped `sync` command that:

- refreshes remote review observations across more than one selected stack
- decides when local bookmark materialization should happen automatically
- coordinates with `cleanup --rebase` without turning refresh into implicit
  history repair

## Git Commit Change-ID Header

_Benefit: unknown — potentially useful for recovery and checkout UX, but not needed for the
current core workflow._

Recent `jj` versions can write a `change-id` header into Git commit objects created by
`jj`. That header is not shown by normal Git or GitHub commit views, and it should not become
a new source of truth for jj-stack. Still, it may be useful evidence in future recovery
flows where the user experience should follow a logical `jj` change rather than one exact
commit object.

High-level cases where this might help:

- importing or rediscovering an existing PR stack when review branch names no longer follow
  jj-stack's generated naming convention
- explaining branch drift when a review branch points at a different commit that may still
  belong to the same logical `jj` change
- reducing unnecessary manual relinking when jj-stack can tell that a GitHub PR branch and
  a local change probably share the same underlying `jj` change identity

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
  states in `view` without introducing forge-owned stack metadata as a
  competing source of truth
- how the queue or landing-PR path should preserve the current fail-closed
  behavior when the ready prefix changes locally while a queued landing is in
  flight
- whether queue-backed landing needs resumable operation data beyond the
  operation-log evidence and tracking state used by the direct-push flow
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

- richer "next command" guidance after `submit`, `land`, `unstack`, and
  `cleanup --rebase`
- clearer distinction between "inspect only", "safe retry", and "history
  rewrite" recovery paths when something is stale or ambiguous
- an explicit guided-recovery flow for common cases such as "ancestor already
  landed", "remote branch disappeared", or "tracking state no longer matches the
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

## Post-Submit Closure Detector — Coverage Gaps

_Benefit: small — the predictor and the existing detector already cover the
loud failure modes; these are residual gaps where state changes are silent
or extremely rare._

The post-submit detector raises when a PR transitions open → closed or
open → missing during `submit`. It does not currently distinguish:

- a PR whose `is_draft` flipped during the run (state stays `"open"` either
  way) — fine for the auto-close case but would not surface a hostile draft
  toggle initiated outside `submit`
- a PR that GitHub closed and a third party reopened mid-run; the detector
  reads the post-run state and considers it clean

If either of these turns out to bite real users, broaden the detector to
compare more fields rather than only state.

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

## Per-Invocation jj Subprocess Overhead

_Benefit: small — the cheap consolidations have been applied; what remains
requires restructuring that has not yet paid for itself._

Applied so far: the `jj --version` gate and `get_config_string` reads are
cached per process/client, and semantic color styles are only loaded when a
console can actually emit color. The remaining fixed per-invocation reads
(`config list jj-stack` as the working-copy snapshot anchor, `git remote
list`, `bookmark list --all-remotes`, plus one `ui.color` read each from
pre-bootstrap console setup and the repo-scoped client) each answer a live
question once and were left alone.

Evaluated and deferred: batching per-revision `jj log -r <rev> --limit 1`
display renders into one call. A combined revset renders a connected graph
(different output than independent per-revision blocks), and splitting one
render faithfully requires wrapping the user's configured log template in
markers. The render path already overlaps its subprocess spawns with a
thread pool, so the win is modest relative to the fragility. Revisit only if
per-revision rendering shows up as real CLI latency.

## External-Drift Model Follow-ups

_Benefit: medium — the drift family covers the reachable single- and dual-drift
combinations for `submit` plus a `view` report smoke; these extensions deepen the
same model rather than change it._

The transition vocabulary and required behaviors live in
[distributed-state.md](distributed-state.md). Deferred extensions:

- drifts targeting orphaned PRs (close or delete-branch on an orphan while
  submitting the surviving stack should stay a success-class scenario with
  adjusted orphan expectations)
- `view --fetch` in the drift replay, which pulls foreign refs into the local
  view and exercises the fetch-artifact tolerance paths
- drift replay against `land`, `cleanup --rebase`, and `unstack`, which have
  their own mutation surfaces and fail-closed obligations
- a tracking-store-loss drift (fresh machine, deleted state file with live PRs)
  once the product decides which proofs let `submit` adopt existing PRs versus
  requiring `checkout`
- an exhaustive enumeration mode for drift pairs at small stack sizes; the
  space is small enough to enumerate outright instead of sampling
- a TLA+ sketch of the transition lattice for oracle-completeness checking was
  considered and deferred: the current vocabulary is shallow, and the valuable
  check is agreement between the model and the real `jj`/CLI/fake-GitHub
  boundary, which a spec cannot replay. Revisit if concurrent commands or
  multi-remote support make interleavings first-class.

## Property Harness Cost Trims

_Benefit: small — the property suite is opt-in, so this only affects the CI
smoke job and manual runs._

Remaining from the test audit: the harness rebuilds and submits each
scenario's initial stack from scratch and could reuse per-size cached
submitted-stack templates the way integration tests now do, at the cost of
aligning the harness's label conventions with the template contents. The
other audit findings (duplicate `insert-before-middle` fixed scenario,
per-label remote-ref reads) have been applied.

## Native GitHub Stack Metadata via `gh stack link`

_Benefit: medium — replaces tool-managed PR comments with GitHub's first-class
stacked-PR UI, but depends on a GitHub feature that is still rolling out._

GitHub's `gh stack` CLI ships a `link` subcommand designed for external branch
managers (jj, Sapling, git-town): it registers an ordered set of PRs as a
server-side stack so GitHub renders native stack navigation in the PR UI.
jj-stack currently projects the same information into PR comments via the
hidden `<!-- jj-stack-navigation -->` and `<!-- jj-stack-overview -->` markers.

Possible follow-up work:

- on submit, register or update the stack via the API behind `gh stack link`
  instead of (or in addition to) writing navigation comments
- drop the navigation comment path entirely once native stacks are broadly
  available, keeping the overview comment only if it still adds value
- decide how `checkout` should treat PRs that are linked into a native GitHub
  stack but have no local tracking data
