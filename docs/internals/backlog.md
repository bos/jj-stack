# Backlog

Items that need to be implemented or thought through, but are not blocking current work.

## Unverified GitHub landing contracts

_Benefit: large — these observations determine whether the fake accurately represents the
external contracts used by landing and recovery._

A disposable live-GitHub experiment would cover:

- direct-push pull-request lifecycle
- retarget-and-close behavior
- `merge_commit_sha` for merge, squash, and rebase methods
- merged-head branch deletion
- rejection of a merge request whose expected head is stale

No live experiment has been run. Until one is separately approved and recorded, fake-dependent
conclusions in these families remain conditional and release evidence must report the gap rather
than treating local tests as proof.

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

Since `jj` 0.30 the `change-id` header in Git commit objects is written and imported by
default (`git.write-change-id-header`), so change IDs survive ordinary push/fetch round
trips. The `jj` changelog notes the limits that matter here: `git rebase` and some forges
drop the header when they rewrite commits, and GitHub squash merges create fresh commits
without it. Selected `sync` therefore proves that a merged ancestor is no longer needed from the
exact submitted commit or live merge-result commit on trunk, not from header identity. Normal Git
and GitHub commit views do not show the header, and it should not become a new source of truth for
jj-stack. Still, it may be useful evidence in future recovery flows where the user
experience should follow a logical `jj` change rather than one exact commit object.

High-level cases where this might help:

- importing or rediscovering an existing PR stack when review branch names no longer follow
  jj-stack's generated naming convention
- explaining branch drift when a review branch points at a different commit that may still
  belong to the same logical `jj` change
- reducing unnecessary manual relinking when jj-stack can tell that a GitHub PR branch and
  a local change probably share the same underlying `jj` change identity

## Additional landing modes and merge queues

_Benefit: medium — high value for teams that require merge queues, but complex
to design correctly and not blocking the current direct-push flow._

The canonical `land` model supports direct push and GitHub PR merge. The remaining product
question is whether it should eventually support a landing PR or merge queue while keeping the
`jj` DAG as the source of truth. Concrete follow-up questions:

- whether `land` should add a landing-PR or merge-queue mode
- how queue-backed landing should report queued, running, failed, and merged
  states in `view` without introducing forge-owned stack metadata as a
  competing source of truth
- how the queue or landing-PR path should preserve the current fail-closed
  behavior when the changes ready to land move locally while a queued landing is in
  flight
- whether queue-backed landing can recover from current state without durable intent or replay
  data
- how repo policy requirements such as required checks, branch protection, and
  review-only `review/*` branches should be diagnosed before a landing attempt

This should be designed explicitly rather than bolted onto the current `land`
flow piecemeal.

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

The bookmark-managed check that decides whether to pass `allow_backwards` to `set_bookmark` has
two arms: a fast path where a matching managed `ReviewIdentity` proves ownership, and a fallback
that asks `jj` whether the bookmark's current local target resolves to the same `change_id` as
the desired commit. The split integration test exercises only the first arm because the prior
submit creates the identity. A focused untracked or external-identity fixture would lock in the
fallback so a future refactor cannot silently break it.

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
- decide whether `--describe-with` should receive richer structured context or remain limited to
  `--pr <revset>` / `--stack <revset>` and JSON output

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
- typed `DriftError` conditions for the remaining untyped fail-closed guards in `submit`,
  added together with the transition that reaches each one: the conflicted-bookmark guards
  become reachable once the replay fetches between drift and submit (the `view --fetch`
  extension above), and the branch-takeover guard is the fail-closed face of
  tracking-store loss or of a remote branch-name collision with a never-submitted change
  (a candidate new drift kind). Until then, a replayed drift that trips an untyped guard
  fails the diagnosis assertion loudly, so nothing is silently absorbed. The atomic-push
  preflight stays out of the model: it fires with review identity fully proven and is a
  local tracking-configuration limit, not drift.
- an exhaustive enumeration mode for drift pairs at small stack sizes; the
  space is small enough to enumerate outright instead of sampling
- reconsider a formal state model only if concurrent commands or multiple remotes make the
  interactions too complex for the current executable scenarios

## Land Property Harness Follow-ups

_Benefit: small — the land families now cover composed edits, external drift,
interrupted retries, and the merged-prefix handoff chain; these extensions deepen the
same models._

- `--via merge` variants of the land drift family (the readiness walk is shared, so
  push-only coverage was chosen first; the mergeability stop adds one more boundary)
- composed drift pairs against land, mirroring the submit drift family's dual-drift
  combinations

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

## Landing cleanup follow-ups

_Benefit: small each; recorded so leaving safe cleanup work remains an explicit decision._

- Direct-push landed review branches are left on the remote by design (the current land flow only
  forgets local bookmarks). A `cleanup` GC pass could delete tool-owned remote branches
  whose PRs were closed as landed, but it would need an exact remote-target lease in addition to
  the existing ownership checks.
- Real GitHub's merged detection after retargeting a directly pushed PR remains part of the
  deferred live-evidence experiment above. Do not change the production landed-state check based
  only on the fake server's auto-merge behavior.
