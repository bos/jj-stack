# jj-stack implementation strategy

[design.md](design.md) defines product behavior. This document records the architectural choices
that are not obvious from reading the source tree.

## Authority and stored state

The `jj` DAG is the only authority for local stack topology. Tracking may annotate a change with
its GitHub PR, but it must never save parent relationships, stack membership, or a replay
path. Repo-wide and selected-stack discovery therefore start from current `jj`
observations.

Tracking stores one atomic pair per tracked change:

- the repo, pull request, and head branch that identify the PR
- the exact commit last successfully submitted or explicitly adopted

The pair is enough to reject uncertain mutations and prove which submitted snapshot is being
discussed. Everything else is observed again. There is no transaction journal, operation phase,
saved selector, or recovery state machine.

All workspaces for one `jj` repo share the same state file and operation lock. Repo
identity comes from the canonical `.jj/repo` storage path, not the workspace path. State writes
use atomic file replacement; the lock coordinates processes but does not make GitHub, Git, and
local storage transactional.

## Observation, planning, and mutation

Commands keep five concerns separate:

1. observe typed local, remote-ref, GitHub, and tracking state
2. classify that state without side effects
3. build the complete plan required before the first mutation
4. apply dependent mutations in order
5. save and render only completed outcomes

Shared code may observe or classify facts for several commands. It must not become a second place
that decides command policy. Command-specific planning stays with the command, while product rules
remain in [design.md](design.md).

Independent reads should be batched or run concurrently. A mutation is re-planned only when an
earlier mutation changes one of its inputs or the external API requires another observation.
Irreversible writes use the identity or version observed during planning whenever the platform
supports a conditional request or lease.

PR-branch changes for one submit use one atomic Git push with an exact lease for every ref,
including expected absence for a new branch. There is no sequential fallback. GitHub mutations
cannot be made atomic with that push or with the state file, so retry safety comes from fresh
observation and the saved PR pair.

## External boundaries

The client invokes `jj` and Git as subprocesses rather than linking to `jj-lib`. Machine-readable
`jj` templates are preferred over parsing display output. Direct Git access is limited to remote
inspection and leased ref mutation that `jj` does not expose with enough precision.

Read-only setup and presentation calls may ignore the working copy. Operations that fetch or
rewrite preserve normal `jj` snapshot and checkout behavior. Remote PR branch refs are inspected
without importing them into the ordinary `jj` view; commands that must attach a remote commit use
a temporary ref and remove it before returning.

When correctness depends on the result of a local rebase before that rebase is allowed to affect
the repo, the jj client uses `--no-integrate-operation`. It inspects the candidate DAG with
`--at-op` and integrates that exact operation only after the caller's proof succeeds. Operation
IDs are transient values within one command and are never saved as recovery state.

GitHub transport owns authentication, pagination, bounded retries, batching, response validation,
and error decoding. It returns typed observations and mutation results but does not decide stack
topology, selection, branch names, or mutation eligibility.

Configuration is read through `jj config` so user, repo, workspace, `--config`, and
`--config-file` precedence stay identical to `jj`'s. Python does not implement a second merge of
those scopes.

Serialized and untrusted data uses `pydantic` models. In-process plans and results use typed
dataclasses where practical. Public `--json` output is a separate interface governed by
[`docs/json-output.schema.json`](../json-output.schema.json), not by the tracking or GitHub
models.

## Documentation generation

The CLI parser and help text are the source for both terminal help and the complete online CLI
reference. `jj-stack help --all-in-one` renders a Markdown fragment with semantic HTML classes;
it does not add site navigation or styling. The website invokes that renderer from the same
jj-stack checkout whose canonical user guides it snapshots, then owns the generated page's front
matter, CSS, and publication.

## Test boundaries

The local integration environment uses real `jj` and Git repos with a purpose-built
FastAPI GitHub server. The fake implements only behavior the client needs, and its branch and
ancestry assertions use a real backing Git repo rather than mocked JSON alone.

Fake behavior should match observed GitHub behavior, including surprising behavior. A known
difference must be documented beside the fake and affected tests. There is no credentialed live
suite; an unverified GitHub assumption remains conditional until a separately approved experiment
runs in a disposable repo.

[testing-philosophy.md](testing-philosophy.md) defines which tests are worth keeping, and
[property-testing.md](property-testing.md) explains the generated integration harness.

## Complexity limits

`complexity-budget.toml` and `tools/check_complexity.py` enforce cumulative production, test,
recovery-module, complexity, and marked-test limits. Increasing a limit requires a design review.
Moving the same policy into another helper or module is not a reduction in complexity.
