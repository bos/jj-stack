# jj-stack implementation strategy

This document covers the implementation choices that follow from the single canonical product
specification, [design.md](design.md). It defines repository layout, component boundaries,
tooling, test strategy, and delivery shape.

The canonical product specification defines behavior. This file records how the current code is
built, not what the product does.

## Summary

We build a Python client that maps a `jj` stack to GitHub's branch-based pull request
model.

The client:

- shells out to `jj` and `git` rather than linking to `jj-lib`
- uses `uv` for environment, execution, and dependency management
- uses `pyrefly` for static type checking
- uses `pydantic` for typed local and remote data models
- uses `httpxyz` for GitHub API traffic

We test behavior against a local fake GitHub server backed by a real Git repository.

We develop the tool the same way we want people to review with it: logical,
self-contained, well-described stacked commits.

## Goals

1. Build a useful tool quickly without painting ourselves into a corner.
2. Let the `jj` DAG determine stack topology.
3. Keep GitHub integration narrow, explicit, and easy to inspect in tests.
4. Prefer end-to-end feature slices over big batches of infrastructure work.
5. Make the local fake GitHub environment the default place to develop and debug
   behavior.
6. Record the fake's idealizations and validate them against real GitHub when an approved live
   experiment exists.

## Non-goals

Product-level scope follows the design doc. Additional implementation non-goals:

- non-GitHub forges
- a daemon or long-running background sync process
- a GUI or web UI

Reviewer and label assignment are in scope for PR creation and update flows.

## Implementation model

Mutating review commands generally follow this shape:

1. Read the required local `jj` and Git state.
2. Compute the desired tracking state.
3. Read relevant GitHub state when the command crosses that boundary.
4. Reconcile actual remote state with desired state.
5. Apply any mutations in a controlled order.
6. Persist only the minimal tracking changes the command actually made.

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

The bundled agent skill in `skills/jj-stack/` is installed separately from the executable. It:

- discovers and caches the working invocation for a repository
- uses `list --json` and `view --json` to recognize locally managed reviews
- routes structural PR and review-branch changes through `jj-stack`, except for the explicit
  `gh stack unstack` repair that dissolves one native GitHub stack before separate submissions

Built-in help and the user guide own the command and alias inventory.

Command entrypoints follow four layering rules:

1. Build one `CommandContext` containing shared configuration, clients, and state storage.
2. Reject argument errors that need no repository state before bootstrap.
3. Resolve and validate a typed command target before mutation.
4. Pass mutation code only the state required for its work.

Argument parsing, shared dependencies, target resolution, and live mutation state stay separate.
Wrapper objects should be deleted once they only forward values to one caller.

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
      _github_stack_support.py
      _native_stack_safety.py
      cleanup/
      merge/
      submit/
        native.py
    jj/
    github/
    review/
      native_sync.py
    state/
tests/
  unit/
  integration/
  property/
  support/
tools/
  check_complexity.py
  check_jj_release_updates.py
  install-jj-release.sh
scripts/
skills/
  jj-stack/
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

Wraps subprocess access to `jj` and exposes typed observations and mutations: resolve a revset,
read bounded revisions with caller-supplied membership flags, inspect working copies and ordinary
bookmarks, and surface stale-workspace errors distinctly so commands can suggest
`jj workspace update-stale`. It detects imported managed review bookmarks only to preserve the
fail-closed boundary around the remote-only review namespace.

The adapter prefers machine-readable template output over parsing human text.
Revision templates capture both `current_working_copy` and the names returned by
`working_copies`: the former marks the invoking workspace, while the latter lets repo-scoped
discovery recognize working-copy commits owned by any workspace.

### Git boundary

There is no separate production Git adapter. `JjClient` uses direct Git subprocesses only for
exact remote-ref leases and remote inspection that `jj` does not expose. Test support owns the
backing-repository and fake-server Git operations.

`JjClient` resolves and caches `jj --ignore-working-copy git root`, then supplies that exact
object store through `git --git-dir` for every direct Git command. Remote inspection uses the
configured fetch URL; leased mutation uses the configured push URL. No raw Git command receives
a configured remote name, and the same boundary works for colocated and non-colocated
repositories.

Ordinary fetch installs a negative Git refspec for the reserved namespace and rejects an effective
jj `fetch-bookmarks` override that would bypass it. Every broad jj import or fetch that jj-stack
performs immediately rechecks for imported managed review bookmarks before callers consume its
result. Recovery ends with `jj git export`, so forgetting an imported bookmark also removes its
raw local or remote-tracking ref. Review observation uses direct `git ls-remote` against the fetch
URL, without importing refs or bookmarks. Explicit `checkout` attachment fetches one exact remote
ref into a fixed temporary Git ref, imports it into jj, verifies the full change ID, and removes
the temporary ref and bookmark in a `finally` path. `relink` instead fetches and reads the exact
remote commit object without creating a ref, then compares its full change ID to the selected
local revision.

Every submit or deletion expresses its complete remote-ref mutation as one direct atomic Git push
to the push URL. Each update carries an exact `force-with-lease` expectation, including expected
absence for a new branch. There is no sequential fallback and no follow-up fetch. The auto-close
predictor therefore evaluates the same one-step ref transition GitHub will observe.

Two crash windows would otherwise strand a stack behind that lease. A tracked branch may also
move from the exact target an interrupted push already left there. An untracked change whose
branch is already on the remote — a first submit that pushed and then failed before recording
anything — is adopted only when exactly one candidate carries a commit whose `change-id` header
is that change; none, or more than one, is ambiguous and fails closed.

### Planning rule

Planning is a layering rule rather than a separate package. Shared classification lives under
`review/`, while command-specific planning lives beside the command, such as
`commands/merge/plan.py`, `commands/merge/native.py`, and the submit modules. Given typed local
and remote state, it decides:

- which changes are reviewable
- which stable remote branch each change should use
- which PR each change should map to
- which remote mutations are required
- which operations are hard errors

Reviewability comes from `jj` state, not tool-local policy: the planner respects the
repo's configured `immutable_heads()` boundary via `jj`'s `immutable()` / `mutable()`
semantics.

Derived per-change review state lives in `review/change_status.py`. `ReviewChangeStatus` is a pure
classifier over local revision state, saved tracking, remote refs, and already-loaded PR data. It
does not load state, choose a stack, or decide whether mutation is safe. Commands apply their own
policy to the result and keep exact identity, baseline, PR, and branch values for concrete
mutations.

This is where most correctness lives.

Selected path planning has a smaller pure boundary in `review/path.py`. The selected adapter
observes selector copies, the ordinary first-parent chain, fetched-trunk membership, and tracking
annotations in one bounded `jj` query. For a full change ID or linked pull request, the pure
projection prefers the unique mutable local copy outside fetched trunk's first-parent path and
stops when matches remain only on that path. A sole immutable reviewed side parent from a native
merge remains outside the path and selectable until `sync`. The projection returns one
parent-connected `LocalStack`. Tracking annotates that path and cannot create or reorder it.
Command-owned GitHub identity, merge evidence, and mutation policy stay outside the projection.

### GitHub client

Thin `httpxyz` wrapper plus typed `pydantic` models. Knows how to fetch PR state, batch PR
lookup by PR number or known head branch, create PRs, update PRs, assign reviewers and labels,
manage navigation and overview comments, list/create/append/unstack native resources, submit and
poll asynchronous native merges, and handle endpoint-specific pagination or retry.

When endpoint semantics allow it, the client and command layers prefer batched or
bounded-parallel GitHub work over one-request-per-item serial loops. Ordering
constraints stay explicit at the command layer when the visible result needs a specific
sequence.

`sync --all` reads submitted-commit ancestry in chunks of up to 200 commits, reads PRs through
GraphQL in chunks of 25, and then checks any reported merge-result commits in another batched
ancestry read. If a GraphQL batch fails, bounded REST requests preserve a separate result for each
PR. Missing commits and failed PR reads therefore remain local to their tracked changes. These
initial reads are diagnostic only: PR updates and local retirement still run one candidate at a
time, with fresh observations before each change.

`submit` predicts GitHub auto-close risk before pushing rewritten review branches. The behavioral
rule belongs to the submission algorithm in [design.md](design.md); the implementation uses one
batched ancestry query over the planned head/base pairs, then pre-retargets only the affected PRs.
Property families and their assertions live in [property-testing.md](property-testing.md).

`submit` batches stack-comment reads by PR number through GraphQL before mutating the
managed comments, falling back to REST pagination only for PRs whose first comment page
is incomplete.

Native group merge is `PUT /repos/{owner}/{repo}/pulls/{target_pr}/merge-async`, whose body
carries `merge_method` and the `sha` of the exact target PR head. An accepted request returns an
operation UUID that the client polls to a terminal state. A concurrent `409` is decoded so a
matching pending request can be distinguished from an unrelated conflict, but its UUID is never
adopted, because the response body does not identify the target PR. The merge policy those
requests serve is specified in [design.md](design.md).

The client reports endpoint results but does not decide capability, stack topology, branch naming,
native membership policy, or fallback behavior.

### Config and tracking state

- config lives in `jj`'s config scopes under the `jj-stack` namespace
- `branch_prefix` names the one reserved branch namespace. `review/namespace.py` resolves it once
  during bootstrap and is read wherever it is needed; it depends on nothing else in the package,
  so `jj/client.py` can name the reservation without importing the policy above it
- repo-specific defaults use `jj`'s built-in user/repo/workspace precedence
- we do not duplicate `jj`'s config resolution in Python: reads go through
  `jj config list 'jj-stack'`, which inherits user/repo/workspace precedence plus
  effective `--config` / `--config-file` overrides on every `jj` invocation
- tracking state lives in
  `${XDG_STATE_HOME:-~/.local/state}/jj-stack/repos/<repo-id>/state.json`
- the state-file envelope also caches one `stacked_pull_requests` boolean for each resolved GitHub
  repository; the enclosing path supplies the local repository half of the cache key
- `<repo-id>` is derived from the canonical `.jj/repo` storage path so every workspace
  for the same repo shares one state location. Primary workspaces expose that path as a
  directory; additional workspaces expose a path file whose contents are resolved relative
  to the workspace's `.jj` directory. The path file's own location is never hashed as the
  repository identity.
- reads treat a missing state file as empty state; writes create parent directories on
  demand and only fail if the filesystem refuses

The repo state directory also contains the operation lock files:

- `operation.lock` is the fixed-path advisory lock sentinel
- `operation-lock.json` is diagnostic companion metadata for the current holder

Mutating commands hold the lock through their mutation phase. Bootstrap, validation, interactive
selection, and final rendering may happen outside it. The lock is process coordination only. The
state directory contains no operation journal, merge note, phase, selector, path, native resource
ID, or recovery checkpoint.

Merge and recovery share current-state observation rather than a durable operation state
machine:

- `commands/merge/` checks and asks GitHub to merge one selected review path
- `commands/sync.py` repairs a selected stack
- `commands/sync_global.py` performs explicit repository-wide recovery
- `review/trunk_evidence.py` distinguishes an exact submitted commit from a rewritten GitHub
  merge result
- `review/finish.py` finalizes merged PRs and removes saved tracking
- `review/convergence.py` checks whether another visible stack still needs that tracking
- `review/native_sync.py` validates historical native members and survivor transitions
- `commands/_github_stack_support.py` owns the one cached capability decision
- `commands/_native_stack_safety.py` owns the one native membership decision:
  `selected_native_stack` resolves the single resource a selected review set belongs to and
  requires every active member of it to be selected. `submit`, `merge`, selected `sync`,
  `unstack`, and cleanup call it and derive their own consequence from the resource it returns;
  none of them repeats the decision

Selected native sync uses the same fixed temporary attachment as checkout for one additional
purpose: after a native merge rewrites the active suffix, it validates every active raw Git commit
and parent, reobserves the whole branch set, then imports the exact top into jj. It rebases only
trailing local descendants, abandons the replaced local active copies, and advances every adopted
baseline together through the state store's existing pair compare-and-swap. It
reobserves the branch set again before leaving the attachment. If that check fails, the updated
baselines let a retry adopt the newer exact chain while historical tracking remains until
survivor submit succeeds. No review bookmark survives; the exact imported commits become the
local unbookmarked survivor chain.

State saves are atomic but not fsync durable. The saved identity prevents action on a different PR
or branch, while the baseline records the exact reviewed commit. If a reconstructible cleanup
write is lost, the next command rereads current state and reports or completes the remaining work.

Tracking state stays minimal and optional. It is a small versioned
JSON file validated through `pydantic`. Human-authored config stays in TOML.
The current top-level state version is 3. Each `ReviewIdentity` is version 3 and contains only
the exact repository, PR, and head-owner/ref fields; `SubmittedBaseline` remains version 1.

Public `--json` command output is a separate user-facing contract. Its schema lives in
`docs/json-output.schema.json`, and integration tests validate actual `view --json` and
`list --json` payloads against that file so the emitters cannot accidentally expose tracking-state
or GitHub-client internals.

Repository-wide discovery supplies current stacks to `commands/list_.py`, which loads tracking
separately. `review/change_status.py` classifies each change and enumerates orphaned records;
`commands/_stale_stacks.py` renders stale-stack advisories. Selected discovery supplies `view` and
mutation targets. All paths preserve malformed or unmatched saved identities for explicit repair
rather than changing them during inspection. The command behavior is specified in
[design.md](design.md).

Orphan cleanup lives in its own command module because it begins from saved identity rather than
a selected live stack. `review/observation.py` batches raw observations of saved identity and
baseline pairs, exact PR numbers, unique head claims, and open base-ref dependents. It does not
create a second exact-PR resolution path.

Ordinary close, selected cleanup, orphan cleanup, sync, and merge request only the facts their
mutation boundary needs; `_close_actions.py` applies the shared exact-link and dependent-PR
eligibility checks instead of observing those facts again through a command-specific path.

Repository-wide cleanup is one lifecycle-driven pass over complete identity/baseline pairs.
It observes the exact saved PR, prepares branch and comment cleanup for a closed or merged match,
and rereads the PR, its unique head claim, open base-ref dependents, remote ref, native
membership, and tracking records at their mutation boundaries. Selected cleanup processes the
observed stack head-to-base. A dry run may omit only dependents that an earlier selected action
would close; actual cleanup never omits a live dependent. Local jj descendants remain
selected-sync evidence, not cleanup evidence. Shared code supplies observation and artifact
mutation without a second set of eligibility rules.

## Data model

Use `pydantic` at serialized or untrusted boundaries: configuration, tracking files, `jj`
template records, and GitHub responses. Use typed dataclasses for in-process plans, results, and
mutable fake-server state. Important model families include:

- local stack models
- remote review-branch models
- GitHub PR and comment models
- mutation plan and result values
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
- GitHub owner/repo: derive from the selected remote's fetch and push URLs, which must identify
  the same repository; SSH host aliases are transport configuration and need not match

Ambiguity is a hard stop, not something the tool guesses past.

## Authentication

GitHub credentials resolve in this order:

- `GITHUB_TOKEN`, if set
- `GH_TOKEN`, if set
- `gh auth token`, if `gh` is installed and authenticated
- otherwise send no authentication header; `doctor` reports the missing token, and ordinary
  commands fail if GitHub rejects the request

The application client uses `httpxyz` directly for GitHub calls. If we reuse `gh`
credentials, we go through the supported `gh auth token` command, not by reading `gh`
config files, keychain entries, or other internal storage.

## Tooling

- `uv` for environment and dependency management
- `uv run` for local command execution
- `uv tool run` only where it clearly improves ergonomics
- `./check.py` as the default local verification entrypoint
- `tokei` for code-line counts, excluding docstrings, enforced with the other cumulative size
  and test-count limits by
  `complexity-budget.toml` and `uv run tools/check_complexity.py`
- `pyrefly` for static type checking
- `ruff` for linting and formatting
- `pytest` for the test runner

## Testing strategy

Testing is the center of the implementation strategy, not an afterthought.

For every user-visible behavior:

1. write tests first
2. implement against the local fake GitHub server
3. identify whether confidence depends on real GitHub behavior
4. when it does, keep the conclusion conditional until an approved live experiment or future
   live test establishes it

Implemented local coverage includes:

- unit tests for parsing, planning, and model behavior
- local integration tests against the fake GitHub server and a real backing Git repo
- six fixed generated/property cases that replay the integration harness in the default suite
- focused merge and recovery cases, including native atomic failure, partial survivor rewrites,
  terminal retry, and ordinary sequential stops

Local tests are the default.

Larger deterministic property pools are opt-in; their families, runner, and reproduction workflow
are documented in [property-testing.md](property-testing.md). CI runs a bounded expanded pool on
one Linux/jj-version combination.

The default local verification command is:

```text
./check.py
```

That script runs `uv sync --locked`, then `ruff check`, `pyrefly check`, a second
`pyrefly check --python-platform win32` pass, and `pytest -n auto` with randomized test
order so hidden cross-test coupling and Windows-only type errors fail fast.

`./check.py -n 4` overrides the default worker count; `./check.py -n 1` provides a
serial escape hatch without changing the bootstrap, lint, and type-check steps.

`./check.py --pytest-concurrency-report` keeps the same bootstrap, lint, and type-check
flow, then runs pytest with a local plugin that measures per-test wall-clock occupancy,
reports average and peak active-test counts, and highlights tests that contribute the
most concurrency debt.

`./check.py --coverage` keeps the same bootstrap, lint, and type-check steps, then runs
pytest with branch coverage enabled, emits a terminal missing-lines report, and writes
an HTML report to `htmlcov/index.html`.

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

Its native endpoints model ordered resource membership, historical merged prefixes, exact active
suffix unstacking, create/append admission, and asynchronous merge submission and polling. The
merge fixtures cover atomic failure, partial survivor rewrites, and terminal retry. They remain
bounded contracts rather than a general native-stack emulator.

We use FastAPI for the fake server unless Starlette later proves to offer a clear
concrete advantage for this test harness.

## Live GitHub evidence

There is no credentialed live suite. Approved disposable-repository experiments established the
native create, append, unstack, historical-member, asynchronous merge, queue-rejection,
merge-method, expected-head, and Git `change-id` contracts modeled by the fake. Future live checks
still require explicit credentials, a disposable repository, and separate approval for external
mutations.

## Development workflow

Because we build a stacked review tool, we use stacked review discipline:

- every implementation slice is logically self-contained
- every commit has a clear purpose and description
- tests for the slice ship with the slice
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

- `jj-stack view`
- `jj-stack relink`
- `jj-stack unstack`
- `jj rebase`
- `jj-stack cleanup`
- `jj workspace update-stale`

Unreadable JSON and invalid top-level shape, version, or envelope fail the load. Individual
malformed or missing identity and baseline records are isolated and reported, so unrelated
reviews remain usable; a command needing the damaged record fails closed until `relink` replaces
it.

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
review branch, a non-open or ambiguous discovered PR, a saved-link mismatch, or a selected merge
stack left off current trunk). The
condition is not printed; it exists so the drift property harness
([distributed-state.md](./distributed-state.md)) can assert that a fail-closed stop
fired for the drift it was aimed at rather than merely with the right exit code.

## Observability

Easy to debug without making normal output noisy:

- concise user-facing output by default
- transient progress must stop before persistent output is written to another stream
- debug logging behind a flag
- request/response logging in debug mode with token redaction
- enough plan logging to explain why a change is being created, updated, skipped, or
  rejected

Tests prefer typed results and semantic output fragments. Exact presentation assertions are
reserved for genuinely stable machine or recovery contracts; the suite does not maintain broad
rendered-output snapshots.

## Definition of done

A feature slice is done only when:

- tests were written first or at least before the behavior was finalized
- the local default suite passes
- any approved live evidence required for the slice has been recorded, or the unsupported claim
  remains explicitly conditional
- docs are updated if user-visible behavior changed
- the implementation lands as a logical stacked-review-quality commit

Any commit that changes code is made only after the relevant tests for that change are
passing.
