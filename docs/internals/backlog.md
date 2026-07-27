# Backlog

Items that need to be implemented or thought through, but are not blocking current work.

## Crash and Interrupt Diagnosis

_Benefit: medium — affects users with failed mutating commands, which is uncommon
but can leave jj, GitHub, and saved tracking data out of sync._

Target retry behavior uses a repo-scoped operation lock and derives from the jj DAG, saved review
identity and submitted baselines, fetched trunk reachability, and live GitHub state. There is no
durable transaction, replay phase, retained path, or operation log.

Possible follow-up work:

- document how to locate the repo state directory when debugging with support

## Git Commit Change-ID Header

_Benefit: unknown — potentially useful for recovery and checkout UX, but not needed for the
current core workflow._

Since `jj` 0.30 the `change-id` header in Git commit objects is written and imported by default
(`git.write-change-id-header`), so change IDs survive ordinary push/fetch round trips. Live GitHub
experiments established that both native and ordinary rebase merge preserve it, while squash
merge does not. Selected `sync` uses a preserved header to recognize the fetched successor;
otherwise it retires the old local change from exact merge-result evidence without relabeling the
landed commit or storing an alias.

The header remains evidence, not a new source of truth. Normal Git and GitHub commit views do not
show it, and exact review identity plus fetched trunk and merge-result checks still authorize
recovery. It may nevertheless help future recovery flows where the user experience should follow
a logical `jj` change rather than one exact commit object.

High-level cases where this might help:

- importing or rediscovering an existing PR stack when review branch names no longer follow
  jj-stack's generated naming convention
- explaining branch drift when a review branch points at a different commit that may still
  belong to the same logical `jj` change
- reducing unnecessary manual relinking when jj-stack can tell that a GitHub PR branch and
  a local change probably share the same underlying `jj` change identity

## Merge queue integration

_Benefit: medium — high value for teams that require queues, but not part of the current merge
contract._

Live evidence shows that GitHub accepts a native asynchronous stack-merge request for processing
under a queue ruleset, then returns a terminal failure requiring the queue. The ordinary PR API
cannot enqueue that native member either. `jj-stack` therefore reports the GitHub rejection and
does not impose repository-wide queue or auto-merge restrictions.

A future queue integration needs an explicit design for current-state observation, user-visible
queued/running/failed states, and safe retry without durable intent, phases, or replay data. It
must not make unrelated review mutations read-only merely because another PR is queued.

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

## Documentation follow-ups

_Benefit: medium — keep task-oriented guides and generated reference material complete as the
command surface changes._

Remaining work:

- generated or semi-generated command reference pages that stay in sync with argparse
- example transcripts captured from the fake GitHub environment
- LLM-friendly exports (`llms.txt` / `llms-full.txt`) once the primary structure is stable

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
- drift replay against `sync` and `unstack`, which have their own mutation surfaces and
  fail-closed obligations
- a tracking-store-loss drift (fresh machine, deleted state file with live PRs) that proves
  ordinary `submit` refuses adoption and explicit `checkout` or `relink` restores identity
- an exhaustive enumeration mode for drift pairs at small stack sizes; the
  space is small enough to enumerate outright instead of sampling
- reconsider a formal state model only if concurrent commands or multiple remotes make the
  interactions too complex for the current executable scenarios

## Property Harness Cost Trims

_Benefit: small — fixed representatives run by default, but expanded randomized pools affect
the CI smoke job and manual runs._

Remaining from the test audit: the submit property harness rebuilds and submits each
scenario's initial stack from scratch and could reuse per-size cached
submitted-stack templates the way integration tests now do, at the cost of
aligning the harness's label conventions with the template contents. The
other audit findings (duplicate `insert-before-middle` fixed scenario,
per-label remote-ref reads) have been applied.

## Submit retry property follow-ups

_Benefit: small — the current family covers the main mutation boundaries; extend it only if the
corresponding failures prove important._

- stack-comment failures
- draft-state and review-rerequest failures
- retry after an external GitHub change between attempts

## Checkout import guard covers only the selected pull request

_Benefit: medium — closes the remaining case where `checkout --fetch` can leave a divergent copy
behind, but needs a decision about when the pull-request chain is read._

`checkout --fetch --pull-request` now reads the selected PR head's change ID before importing and
stops when a visible local revision already holds that change at another commit. The check covers
the selected head only. Reproduced remaining case: on a two-PR stack, rewrite the bottom change
and then abandon the top change. The selected head's change is no longer visible locally, so the
guard returns early, the import proceeds, and the bottom change ends up permanently divergent with
every rerun failing identically.

Rewriting a change also rewrites its descendants, so the ordinary rewrite-since-submit case is
already caught: the selected head's commit differs too. Reaching the gap needs the selected
change absent or hidden while a lower one survives in rewritten form.

Covering every head means reading the pull-request chain before the import. That walk currently
runs after it, and the chain read that follows the import is the one that authorizes the tracking
write, so a pre-import walk would either duplicate those requests or move the authorizing read
earlier. Decide which before implementing.

## Rename the reserved review branch namespace to `jj-stack/`

_Benefit: medium — `review/` is a plausible name for a user's own branches, and the whole
namespace is reserved, so a collision costs the user a branch that silently stops updating._

The reserved namespace is `review/`. The fetch exclusion covers all of it and the
imported-bookmark guard reports all of it, so any branch a user keeps under `review/` is refused
or stops being fetched. `jj-stack/` would make that reservation almost collision-free while
keeping the same one-namespace rule.

Deferred because the change is mechanical but wide, and because it resets live tracking:

- every saved `ReviewIdentity.head_ref` starts with `review/`, and the state store validates head
  refs against the managed grammar on every read, so existing records become malformed. Per the
  tracking-state rules they are reported and isolated with `relink` guidance rather than dropped,
  but anyone with live stacks re-adopts them
- existing remote `review/*` branches become unmanaged and need deleting by hand
- the prefix constant is one line; the hardcoded branch strings across the tests and every doc
  mention are the bulk of the work

No migration or compatibility shim should be written for this; the project has no such burden.

## Plain cleanup aborts on one record's lease rejection

_Benefit: medium — one unrelated review branch moving on the remote currently stops cleanup for
every other record, against the stated per-record isolation._

`cleanup` is specified to isolate individual failing records: "Malformed, obsolete, absent,
ambiguous, or individually failing records are reported and skipped without blocking independent
cleanup work." A `DriftError` raised by the leased remote-ref deletion is caught nowhere on the
repository-wide path, so it propagates and ends the whole command. A single review branch that
moved externally therefore blocks cleanup of records that were independently authorized.

`tests/integration/test_cleanup_command.py` pins the abort for a single-record repository, which
is why the gap is invisible: with one record there is nothing else to block.

The fix is a per-record boundary around the branch deletion that records a blocked action and
continues, matching how the surrounding authorization failures already behave. Note the removed
`if not cleanup_current: return False` fallbacks were the vestige of exactly this skip, never
wired up; the missing piece is the guarantee, not those returns.

