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
(`config list jj-stack`, `git remote list`, `bookmark list --all-remotes`, plus
one `ui.color` read each from pre-bootstrap console setup and the repo-scoped
client) each answer a live question once and were left alone.

Evaluated and deferred: batching per-revision `jj log -r <rev> --limit 1`
display renders into one call. A combined revset renders a connected graph
(different output than independent per-revision blocks), and splitting one
render faithfully requires wrapping the user's configured log template in
markers. The render path already overlaps its subprocess spawns with a
thread pool, so the win is modest relative to the fragility. Revisit only if
per-revision rendering shows up as real CLI latency.

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

- overview-comment failures
- draft-state and review-rerequest failures
- retry after an external GitHub change between attempts

## `sync` strands a review branch that `cleanup` can no longer remove

_Benefit: medium — leaves branches in the reserved namespace with no supported way to delete
them._

Observed on a real repository with a single-change stack whose PR had merged. `jj-stack sync
<head>` reported `remove tracking for <change>` but left `jj-stack/<slug>-<change>` on the remote.
`cleanup` then reported `No cleanup actions needed` and `list` reported `No stacks`, because
cleanup verifies branches through the saved tracking that `sync` had just removed. The branch had
to be deleted outside `jj-stack`.

`README.md` says `sync` cleans up a merged PR when no PR above still needs its review branch, and
`docs/troubleshooting.md` sends users to `sync` then `cleanup` for exactly this symptom, so both
the behavior and the docs are wrong about one of the two commands owning the deletion.

Decide which command deletes the branch and make the other one's docs match. Retiring tracking
before the branch it identifies is deleted is the ordering that produces the leak.
