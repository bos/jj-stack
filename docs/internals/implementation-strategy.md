# jj-stack implementation strategy

This document covers the implementation choices that follow from
[design.md](./design.md): repository layout, component boundaries, tooling, test strategy,
and delivery shape.

[design.md](./design.md) is the canonical product spec. This file is about how we build
the tool, not what it does.

## Summary

We build a Python client that maps a `jj` stack to GitHub's branch-based pull request
model.

The client:

- shells out to `jj` and `git` rather than linking to `jj-lib`
- uses `uv` for environment, execution, and dependency management
- uses `pyrefly` for static type checking
- uses `pydantic` for typed local and remote data models
- uses `httpxyz` for GitHub API traffic

We test every behavior first against a local fake GitHub server backed by a real Git
repo, and then against a real GitHub test repo in an opt-in live mode.

We develop the tool the same way we want people to review with it: logical,
self-contained, well-described stacked commits.

## Goals

1. Build a useful tool quickly without painting ourselves into a corner.
2. Keep the `jj` DAG as the source of truth for stack topology.
3. Keep GitHub integration narrow, explicit, and easy to inspect in tests.
4. Prefer end-to-end feature slices over big batches of infrastructure work.
5. Make the local fake GitHub environment the default place to develop and debug
   behavior.
6. Continuously validate the fake environment against real GitHub.

## Non-goals

Product-level scope follows the design doc. Additional implementation non-goals:

- non-GitHub forges
- a daemon or long-running background sync process
- a GUI or web UI

Reviewer and label assignment are in scope for PR creation and update flows.

## Implementation model

Each command follows the same shape:

1. Read local `jj` and `git` state.
2. Compute the desired tracking state.
3. Read relevant GitHub state.
4. Reconcile actual remote state with desired state.
5. Apply mutations in a controlled order.
6. Persist only minimal tracking state and user-authored overrides.

Keep code separated along these boundaries so that planning logic can be tested without
network or subprocess side effects.

## Executable surface

The tool ships as a standalone executable. During development, the default invocation is:

```text
uv run jj-stack ...
```

Users may also configure `jj` aliases that delegate to the standalone executable so
`jj stack ...` works ergonomically. That alias layer is convenience glue, not a
separate implementation surface.

`completion <bash|zsh|fish>` is auxiliary CLI glue: it renders shell completion scripts
from the argparse surface and does not require repo bootstrap, tracking state, or
GitHub access.

The curated top-level help is part of that executable surface. `jj-stack help --all`
shows the full command list and includes any short command aliases so they stay
discoverable without reading the README first.

The bundled agent skill in `skills/jj-stack/` is also a supported delivery surface, but
it is not installed by the `jj-stack` executable. It follows the Agent Skills
`skills/*/SKILL.md` repository convention, so users install it separately with
`gh skill install bos/jj-stack jj-stack` or, during local development,
`gh skill install . jj-stack --from-local ...`. The skill teaches agents to resolve and
cache the repo's working invocation, whether that is `jj-stack`, `uv run jj-stack`, or a
`jj` alias such as `jj stack` or `jj stk`; to check, before direct `gh` PR or branch
mutations, whether `jj-stack` already manages review state in that repo; to cache that
answer for the session; to use machine-readable `list --json` and `view --json` output
for stack ownership checks; and to distinguish ordinary PR collaboration metadata from
structural or lifecycle `gh` mutations that can disrupt jj-stack-managed PRs or review
branches unless the user has seen the risk and approved the direct mutation.

Current aliases include `ls` for `list`, `sub` for `submit`, and `delete` for `unstack`.
Commands that select one linked pull request also accept `-p` as a short form for
`--pull-request`.
When an option has the same user-facing meaning as `gh stack`, prefer the same spelling;
for example, `submit --open` marks draft pull requests ready for review.

Command entrypoints bootstrap a `CommandContext` containing config, the `jj` client,
repo root, runtime options, and the repo state store. CLI boundary code should preserve
that context until it builds command-specific options or resolved target data, instead
of reconstructing shared dependencies from the repo root.

Command-specific options should hold normalized CLI values after argparse-specific
parsing is complete. Commands with their own behavior flags or selectors use
command-specific option values at their orchestration boundaries, with `CommandContext`
carrying shared runtime dependencies. Command code should use the context's state store
rather than reconstructing one from the repo root.

Commands that need nontrivial selection or validation carry that result as an explicit
resolved/prepared target value before mutation. The prepared value should hold the
selected stack or revision, GitHub and remote observations, parsed options, and the
`CommandContext` when later phases need shared dependencies. Mutating phases that need
dry-run mode, journal state, or interim saves use a small run object such as
`SubmitMutationRun`, `LandMutationRun`, `_OrphanCloseRun`, or `AbortRun`.
Submit resolves prepared PR and stack descriptions while loading local inputs, before
bookmark, remote, or GitHub mutation, so bad revsets, duplicate targets, and unreadable
files fail closed.
Purely local mutations should stay out of GitHub inspection paths when they can resolve
their target from `jj` and saved state alone; for example, `unstack --local` only removes
saved tracking records for the selected stack and does not enter the close/cleanup stream.

Do not append command-by-command wiring notes here as new helpers are converted. The rule
is the stable part: keep argparse values, shared dependencies, selected targets, and live
mutation state separate, then delete wrapper objects once they only forward those pieces
to a single caller.

## Repository layout

```text
pyproject.toml
uv.lock
src/
  jj_stack/
    __init__.py
    cli.py
    config.py
    ...
    models/
    commands/
    jj/
    git/
    github/
    planning/
tests/
  unit/
  integration/
  live/
  fixtures/
tools/
  fake_github/
docs/
  mental-model.md
  daily-workflow.md
  troubleshooting.md
  internals/
```

The package name is `jj_stack`.

## Components

### CLI layer

Thin. Parses arguments, loads configuration, initializes logging, builds command
dependencies, and renders user output. Contains no stack planning logic.

Bootstrap failures (missing config, invalid config syntax, bad stack selection) surface
as targeted CLI diagnostics, not Python tracebacks.

### `jj` adapter

Wraps subprocess access to `jj` and exposes typed operations: resolve a revset, inspect
the working-copy/default submit target, enumerate the linear review chain, read
bookmarks plus tracked and untracked remote bookmark state, and surface stale-workspace
errors distinctly so commands can suggest `jj workspace update-stale`.

The adapter prefers machine-readable template output over parsing human text.

### Git adapter

Narrower than the `jj` adapter. We mainly need it for backing-repo inspection in tests,
remote branch verification, fake-server internals, and a few compatibility checks where
Git is the actual remote boundary.

### Planning layer

Pure (or close to). Given typed local and remote state, decides:

- which changes are reviewable
- which bookmark each change should use
- which PR each change should map to
- which remote mutations are required
- which operations are hard errors

Reviewability comes from `jj` state, not tool-local policy: the planner respects the
repo's configured `immutable_heads()` boundary via `jj`'s `immutable()` / `mutable()`
semantics.

Derived per-change review state lives in `review/change_status.py`. The
`ReviewChangeStatus` classifier is observational only: it names the local, saved-link,
remote-branch, remote-target match, PR-lifecycle, draft, review-decision,
submitted-baseline, and saved-identity axes from data the caller already loaded.
Commands can build policy helpers on top of those axes, but mutation code still writes
the underlying tracking fields directly.

The classifier migration is intentionally incremental. Read-side status summaries and
advisories, status cache persistence decisions, cleanup stale-change and rebase planning,
close cleanup planning, bookmark discovery and matching, unlink active-link checks, relink
remote validation, checkout remote validation and branch refresh decisions, submit
untracked-remote repair, submit metadata sync, failed-submit artifact observation, and
land trunk/revision readiness checks consume these axes.
Direct reads of `CachedChange`, `PullRequestLookup`, and bookmark target fields remain
where code is copying underlying data into tracking state, rendering concrete GitHub
payload details, or applying mutations that need the exact target value.

This is where most correctness lives.

### GitHub client

Thin `httpxyz` wrapper plus typed `pydantic` models. Knows how to fetch PR state, batch PR
lookup by known head branch, create PRs, update PRs, assign reviewers and labels, manage
stack-summary comments, and handle endpoint-specific pagination or retry.

When endpoint semantics allow it, the client and command layers prefer batched or
bounded-parallel GitHub work over one-request-per-item serial loops. Ordering
constraints stay explicit at the command layer when the visible result needs a specific
sequence.

Before `submit` pushes rewritten review branches, it predicts which open PRs would
be auto-closed by GitHub's reachability-based merge detection: for each pending PR
whose head ref is in the push set, it computes the post-push commit IDs of head and
base — using `jj`'s ancestor revset against the planned new commits — and pre-retargets
every PR whose post-push head would be reachable from its post-push base to the
resolved trunk branch. The normal post-push PR sync restores the final stacked base.
This generalizes the earlier heuristic ("base is a review branch in the submitted
stack and differs from the new desired base") so that anomalous cases — for example,
a non-stack base that already contains the head — are handled by the same code path.
The opt-in property coverage exercises the user-visible semantics for representative
linear stack edits: moving individual changes, inserting above or below existing
changes, rewriting a change, squashing a change into its predecessor, and abandoning a
change while preserving the orphaned PR. A separate cross-stack split oracle exercises
suffix moves that leave a deferred live stack behind, proving the selected submit does
not mutate that deferred stack's PRs or saved tracking. A stack-merge oracle exercises
two independently submitted stacks merged into one selected linear stack, proving PR
identity and approvals follow `change_id` across the new combined chain. A stack-move
oracle exercises moving one change between independently submitted stacks, proving the
destination stack adopts that change's existing PR while the source remainder is left
untouched. A failed-submit retry oracle injects one-shot failures after remote branch
push, PR creation, PR update, and metadata label sync, then proves a rerun
converges without duplicate PRs. An external-drift oracle generates scenarios from the
typed transition vocabulary in [distributed-state.md](distributed-state.md): after an
initial submit and an optional stack edit, it perturbs GitHub PR state, remote refs,
saved tracking, or the local `jj` view through user-reachable transitions, then asserts
the model-predicted outcome — fail closed with every boundary untouched, or full
success — and that `view` still reports on the drifted state. A land oracle starts from
submitted, partially approved stacks that may have been edited since their last submit;
it predicts the prefix land's readiness walk consumes and models the transport split —
direct-push land retires the landed tracking, merge-transport land keeps merged tracking
so follow-up sync or cleanup can rebase the local stack.

`submit` batches stack-comment reads by PR number through GraphQL before mutating the
managed comments, falling back to REST pagination only for PRs whose first comment page
is incomplete.

It does not decide stack topology or branch naming.

### Config and tracking state

- config lives in `jj`'s config scopes under the `jj-stack` namespace
- repo-specific defaults use `jj`'s built-in user/repo/workspace precedence
- we do not duplicate `jj`'s config resolution in Python: reads go through
  `jj config list 'jj-stack'`, which inherits user/repo/workspace precedence plus
  effective `--config` / `--config-file` overrides on every `jj` invocation
- tracking state lives in `~/.local/state/jj-stack/repos/<repo-id>/state.json`
- `<repo-id>` is derived from the canonical `.jj/repo` storage path so every workspace
  for the same repo shares one state location
- reads treat a missing state file as empty state; writes create parent directories on
  demand and only fail if the filesystem refuses

The repo state directory also contains the operation lock files:

- `operation.lock` is the fixed-path advisory lock sentinel
- `operation-lock.json` is diagnostic companion metadata for the current holder
- `operation-log.jsonl` is the repo-level chronological audit log

Mutating commands hold the lock through their full command lifetime. `view` uses the
non-blocking path only around its best-effort cache write, so live inspection still renders
while another mutation is running. The operation lock replaces same-kind PID waits.

`land`, `submit`, `relink`, `cleanup`, `cleanup --rebase`, `unstack`, and orphaned
`unstack --cleanup --pull-request` append `begin`, mutation, saved-state, and `completed`
events to `operation-log.jsonl` for after-the-fact inspection. `sync` composes
`cleanup --rebase` and `submit` under one operation lock; each composed phase journals
as itself. Retry behavior derives from
the current jj DAG, saved tracking data, GitHub state, and explicit user selectors. The
log is primarily audit evidence and is not a generic recovery model; the one carve-out is
`land`, which consults the log to detect that an unfinished run already pushed trunk so a
rerun finishes the remaining work instead of attempting trunk movement a second time. To
keep that single recovery path reliable across crashes without slowing every audit append,
trunk-moving `land` runs fsync the begin event, the pushed-trunk event, and the completed
marker. When a durable append creates `operation-log.jsonl`, it also fsyncs the state
directory on platforms that support directory fsync.
Direct-push `land` keeps temporary landed tracking while finalizing PRs, then removes the
landed records from active tracking after finalization has succeeded. A rerun that finds
a landed record missing trusts it only when the interrupted operation's journal recorded
the retirement; otherwise it fails closed.

Tracking state stays minimal, optional, and non-authoritative. It is a small versioned
JSON file validated through `pydantic`. Human-authored config stays in TOML.

Public `--json` command output is a separate user-facing contract. Its schema lives in
`docs/json-output.schema.json`, and integration tests validate actual `view --json` and
`list --json` payloads against that file so the emitters cannot accidentally expose tracking-state
or GitHub-client internals.

Repo-scoped inspection treats orphan-only tracking as first-class output. `list` can
render those saved orphan rows directly without loading bookmark state when no live
stacks remain.
The text `list` renderer keeps stack rows compact by rendering the exact PR number for
single-PR stacks and a count for multi-PR stacks; the JSON output remains per-change so
clients can still recover the full PR list.
Unknown saved PR state is treated as open only when the record has a saved PR number.
Records without a PR number are not actionable orphan PRs and can be pruned by cleanup.
Remote branch cleanup still requires a saved PR number, because without one the tool
cannot prove whether an open PR still uses the branch.
When `view` cannot find a PR by the remembered review branch, it falls back to
the saved PR number before rendering the result. A missing branch lookup does not
clear the saved PR identity; read-only status preserves that recovery evidence so
the user can choose between reopening, relinking, or running `submit --restart` to
create fresh PRs. The standalone `restart` command and `submit --restart` share the
same state-reset planner, but `submit --restart` keeps the reset in memory until submit
successfully creates replacement PR identity.
Repo-scoped discovered stacks carry both their immediate base parent and the resolved
`trunk()` revision, plus whether the base parent is on the trunk lineage. Topology-pointer
checks compare the bottom tracked change against that actual DAG parent when the parent
is another mutable review change, while still treating `trunk()` and its ancestors as no
review parent.
Repo-scoped stale-stack detection also compares each tracked change's saved
`last_submitted_commit_id` against the live commit ID, so a rewrite in the same stack
position still prompts the user to inspect and resubmit. `view` renders the same selected-stack
disagreement inline, including whether the saved submit baseline differs because of a local commit
rewrite, a changed review parent, or changed stack membership.
Plain `view` does not run repo-scoped stale-stack discovery. Its "other stack changed" advisory
is limited to stacks built on top of the stack being rendered; use `list` for the repo-wide view.
Plain `view` also does not inspect managed stack-summary comments. That keeps status from doing
one issue-comment request per open PR; `submit`, `unstack`, and `cleanup` own stack-comment
validation when they mutate those comments.

Orphaned `unstack --cleanup --pull-request` uses the same bookmark and stack-comment
validation as regular close before it mutates GitHub state or prunes saved tracking.
It verifies the saved PR identity by PR number, then verifies that the PR head is the
saved branch on the configured GitHub repository before using head-branch lookup only
to detect duplicate live claims. This lets merged orphan PRs be retired without
mistaking a same-named fork branch for the review branch.
The orphan path lives in its own command module because it is a saved-state recovery
flow rather than normal stack close planning; close action rendering and managed
stack-comment lookup stay in a shared helper used by both paths.

## Data model

Define `pydantic` models early and use them consistently across the real client and the
fake server. Important model families:

- local stack models
- bookmark and remote-branch models
- GitHub PR and comment models
- mutation plan models
- config and tracking-state file models

Repo defaults used for resolution belong in config, not in tracking state.

Command output and planning results use first-class typed models. Rendered output is
derived from those models rather than carrying ad hoc dicts or stringly typed
intermediate state through the command layer.

## Default repo resolution

The common case is zero-config. The tool prefers repo-derived defaults and only requires
explicit configuration when the repo is ambiguous.

Resolution order:

- selected remote: `origin` if it exists, then the only remote if exactly one exists,
  otherwise fail
- trunk branch: the selected remote's default branch if it can be found, then one remote
  bookmark on the selected remote that points at `trunk()`, otherwise fail
- GitHub host/owner/repo: derive from the selected remote URL, otherwise fail

Ambiguity is a hard stop, not something the tool guesses past.

## Authentication

GitHub credentials resolve in this order:

- `GH_TOKEN`, if set
- `GITHUB_TOKEN`, if set
- `gh auth token --hostname <resolved-github-host>`, if `gh` is installed and
  authenticated
- otherwise fail with an explicit authentication error

The application client uses `httpxyz` directly for GitHub calls. If we reuse `gh`
credentials, we go through the supported `gh auth token` command, not by reading `gh`
config files, keychain entries, or other internal storage.

## Tooling

- `uv` for environment and dependency management
- `uv run` for local command execution
- `uv tool run` only where it clearly improves ergonomics
- `./check.py` as the default local verification entrypoint
- `pyrefly` for static type checking
- `ruff` for linting and formatting
- `pytest` for the test runner

## Testing strategy

Testing is the center of the implementation strategy, not an afterthought.

For every user-visible behavior:

1. write tests first
2. implement against the local fake GitHub server
3. verify against the live GitHub test repo
4. keep live behavior as the final arbiter

Three layers of tests:

- unit tests for parsing, planning, and model behavior
- local integration tests against the fake GitHub server and a real backing Git repo
- opt-in live tests against a real GitHub repo

Local tests are the default.

Property-style submit stack exploration is opt-in because larger scenario budgets are
intentionally heavier than the default check. Run it by hand with:

```text
tests/run_submit_property_scenarios.py 500
```

The runner reuses the fake GitHub integration harness, generates deterministic stack-edit
scenarios, and runs them through pytest-xdist so the work can spread across available
cores.

CI runs a small submit-property smoke budget on one Linux/jj-version combination so the
generated stack-edit, cross-stack, and retry oracles cannot silently rot while keeping the
full matrix focused on `./check.py`.

The default local verification command is:

```text
./check.py
```

That script runs `uv sync --locked`, then `ruff check`, `pyrefly check`, and
`pytest -n auto` with randomized test order so hidden cross-test coupling fails fast.

`./check.py -n 4` overrides the default worker count; `./check.py -n 1` provides a
serial escape hatch without changing the bootstrap, lint, and type-check steps.

`./check.py --pytest-concurrency-report` keeps the same bootstrap, lint, and type-check
flow, then runs pytest with a local plugin that measures per-test wall-clock occupancy,
reports average and peak active-test counts, and highlights tests that contribute the
most concurrency debt.

`./check.py --coverage` keeps the same bootstrap, lint, and type-check steps, then runs
pytest with branch coverage enabled, emits a terminal missing-lines report, and writes
an HTML report to `htmlcov/index.html`.

Live tests require an explicit flag and explicit credentials.

## Fake GitHub server

The fake GitHub server is a core part of the development strategy.

It:

- exposes only the endpoints we currently need
- models GitHub behavior closely enough to exercise real client logic
- is backed by a real Git repository
- lets tests assert directly on backing Git state after API calls
- evolves incrementally as new client features require more GitHub behavior

This is not a general-purpose GitHub emulator. It is a purpose-built contract test
harness for this tool.

Rules:

- every endpoint corresponds to a real GitHub endpoint we expect the client to call
- fake behavior is written to match observed GitHub behavior, not our preferred behavior
- when real GitHub behavior is surprising, tests document the surprise
- if the fake server knowingly diverges from GitHub, the divergence is called out in the
  tests and in the server code

The fake server owns a real Git repo because many assertions are about actual remote
branch state, not just JSON responses.

We use FastAPI for the fake server unless Starlette later proves to offer a clear
concrete advantage for this test harness.

## Fake GitHub parity tests

We have tests for the fake layer itself to verify that its behavior actually matches
GitHub for the subset of functionality we rely on.

These tests compare observable behavior, not implementation details:

- creating a PR creates the expected remote refs and returns the expected JSON shape
- updating a PR changes the same fields GitHub changes and leaves alone the same fields
  GitHub leaves alone
- comment creation and update behave like GitHub for the endpoints we use
- branch and PR visibility in API responses match GitHub for the scenarios we cover

Where practical, parity tests run the same client action once against the fake server
and once against a live throwaway GitHub repo, then compare the resulting normalized
observations.

## Live GitHub test strategy

The live suite exists from early on, even if small.

Its purpose is not exhaustive coverage. It is to catch fake-server drift and real-forge
edge cases early.

The live suite:

- runs only when explicitly requested
- creates a throwaway repo per run
- uses a dedicated namespace for temporary branches and PR artifacts
- cleans up after itself as aggressively as practical
- avoids touching anything outside its namespace

The first pass uses:

```text
uv run pytest tests/live --live-github
GITHUB_TOKEN=...
JJR_GITHUB_TEST_REMOTE=origin
```

The live suite may use the `gh` CLI for throwaway repo setup and teardown when that
makes the tests materially simpler. We do not use `gh` in the main application client.

## Development workflow

Because we build a stacked review tool, we use stacked review discipline:

- every implementation slice is logically self-contained
- every commit has a clear purpose and description
- tests for the slice land with the slice
- any code change passes its relevant tests before the commit
- docs move with behavior, not weeks later

We prefer:

1. targeted design or strategy update when behavior or assumptions change
2. failing tests
3. minimal implementation
4. cleanup/refactor if needed
5. final docs sync if user-facing behavior or usage changed

rather than a big-framework / big-feature / delayed-everything sequence.

## Documenting changes before coding

When we discover a design bug or behavioral ambiguity, write the intended fix down
before implementing it.

- update [design.md](./design.md) first if the change affects product behavior,
  persistence boundaries, invariants, or user-visible semantics
- update this file if the change is primarily about execution strategy, staging, or
  component boundaries
- use the commit message to summarize what landed, not as the primary place where the
  design decision lives

For small bug fixes, a short targeted edit to the relevant section is enough. We do not
need a new note for every issue. The important thing is that the canonical docs reflect
the intended behavior before code starts depending on a new assumption.

## Error handling

Errors should be explicit and actionable.

User-visible failure cases are defined in [design.md](./design.md). The implementation
classifies them cleanly and surfaces targeted recovery actions.

We distinguish between:

- user/actionable errors
- unsupported-shape errors
- remote state conflicts
- fake-server parity failures
- tool bugs

When possible, diagnostics point to the exact recovery action:

- `jj stack view --fetch`
- `jj stack submit --restart`
- `jj stack restart`
- `jj stack relink`
- `jj stack unstack`
- `jj rebase`
- `jj stack cleanup`
- `jj workspace update-stale`

Unreadable or partially written tracking state is treated as missing data with one
warning, then commands fall back to rediscovery where the design allows.

Process exit codes are formalized and implemented; the contract lives in
[design.md](./design.md) ("Exit codes") with the user-facing table in
[docs/exit-codes.md](../exit-codes.md). Error classes carry their category code:
`CliError` subclasses (usage, ambiguous selection, conflicted stack, unsupported stack)
and adapter errors such as the GitHub client's declare theirs, and `resolve_exit_code`
in `errors.py` maps a raised error to the process exit code, letting a generic
`CliError` inherit the code of a categorized adapter cause. `view` and `list` return the
incomplete-report code directly when a printed report is degraded.

Fail-closed verification stops share exit code 1, so `DriftError` in `errors.py` also
carries a `condition` naming which cross-system check failed (a missing or moved remote
review branch, a non-open or ambiguous discovered PR, a saved-link mismatch, an unlinked
change). The condition is not printed; it exists so the drift property harness
([distributed-state.md](./distributed-state.md)) can assert that a fail-closed stop
fired for the drift it was aimed at rather than merely with the right exit code.

## Observability

Easy to debug without making normal output noisy:

- concise user-facing output by default
- debug logging behind a flag
- request/response logging in debug mode with token redaction
- enough plan logging to explain why a change is being created, updated, skipped, or
  rejected

Tests primarily assert on typed plan objects. Snapshot tests are used sparingly for
user-facing rendered output where the exact textual shape is part of the contract.

## Definition of done

A feature slice is done only when:

- tests were written first or at least before the behavior was finalized
- the local default suite passes
- relevant live GitHub tests pass
- docs are updated if user-visible behavior changed
- the implementation lands as a logical stacked-review-quality commit

Any commit that changes code is made only after the relevant tests for that change are
passing.

## Bottom line

Optimize for a tight loop:

- write a failing test
- implement the smallest real slice against the fake GitHub server
- verify the slice against real GitHub
- land it as a clean stacked commit

If we keep the `jj` DAG as the source of truth, keep the GitHub layer narrow, and keep
the fake server honest by regularly checking it against real GitHub, the implementation
should stay understandable and correct as it grows.
