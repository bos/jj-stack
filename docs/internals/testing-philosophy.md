# Testing philosophy

Tests should protect behavior or constraints that would matter if they broke. Keep a focused
suite whose failures identify useful regressions.

## Gate for every test

A test is worthwhile only if it protects at least one of:

- important user-visible behavior
- a core invariant from [design.md](design.md) or [AGENTS.md](../../AGENTS.md)
- a hard constraint imposed by `jj`, GitHub, subprocesses, or local persistence
- a plausible regression, partial failure, or recovery path

Before adding or retaining a case:

1. Name the user-reachable failure and its practical harm.
2. Search unit, integration, property, and any approved live evidence for overlapping coverage.
3. Explain what distinct bug this case would catch.
4. Choose the cheapest layer that exposes that bug.

Consolidate cases that exercise the same decision at the same layer. Keep coverage at another
layer only when it catches an additional integration or adapter risk. Parameter rows and fixed
generated scenarios count as separate cases. Fixtures, helpers, and generators must support useful
cases; their own complexity does not justify more tests.

## Prefer realistic failures

The main risks in this project are disagreement among the local `jj` DAG, remote refs, GitHub, and
local tracking. Useful cases include:

- configuration lookup failures, invalid values, or settings inconsistent with the repo
- ordinary rewrites, relinks, divergence, conflicts, and nonlinear history
- interrupted commands and partial cleanup
- a supported command or documented external action following another before all systems agree
- recovery after a command detects inconsistent state

Do not add coverage for a state merely because it is imaginable. Require an ordinary supported
workflow, an observed failure, or documented platform behavior that can reach it. Prefer one
representative over a large cross-product matrix.

Usually skip:

- corrupt internal records that no supported command can write
- pathological configuration outside the product contract
- contrived operation interleavings with no observed trigger
- third-party failures the tool cannot handle or recover from
- several tests that restate the same rule at different layers

## Test outcomes, not mechanisms

Assert what a user or external system can observe: the `jj` DAG, GitHub state, remote refs, exit
codes, and useful diagnostics. Avoid pinning private phases, helper calls, request order, or saved
fields unless that internal value is itself the safety boundary under test.

For interrupted operations, prefer a test that interrupts the command, runs the documented retry,
and checks the final state and external effects. If every interruption point needs different
recovery, treat that as a design problem rather than expanding the test matrix.

Build fixtures through supported commands, documented user actions, or realistic external
mutations. Hand-written internal state is appropriate only when testing state-file validation or
another explicit persistence contract.

When the product removes a guarantee or mechanism, remove tests that exist only to preserve it.
Existing tests are evidence of past intent, not a reason to keep unnecessary behavior.

## Choose the right layer

- **Unit or component tests** cover parsing, planning, models, and adapters with controlled
  collaborators. Temporary files and in-process HTTP transports can still belong here.
- **Local integration tests** run the CLI with real `jj` and Git repos and the fake GitHub
  server. Use them when confidence depends on revsets, DAG or workspace behavior, subprocesses,
  or cross-system transitions.

Live GitHub checks are a release gate, invoked separately from `just check` and CI's local tests.
They create and delete a disposable GitHub repo; see
[releasing.md](releasing.md#qualify-the-candidate) for prerequisites. They supplement
deterministic local coverage. Record known fake-server differences beside the affected behavior
and test.

If a behavior has both component and integration risk, keep one representative integration test
and only the unit cases that protect additional decisions. CLI parsing tests are useful when
parsing, normalization, rejection, or selector precedence is the behavior at risk; aliases do not
need separate forwarding tests.

## Keep the suite useful

`just test` runs the full suite with parallel workers. Arguments replace that default, so
`just test tests/unit/test_jj_client.py` runs a focused selection serially; add `-n auto` to
parallelize a selection, or use `just test -n 0` for a serial full run. `just check` also runs
the full suite in parallel, after linting and type checking. Generated command sequences run
only through `just property` and CI's smoke job; see [property-testing.md](property-testing.md).

Measure idle workers with `just check --pytest-concurrency-report`, or use
`just test -n auto --concurrency-report --durations=20 --randomly-seed=1234` to compare test
changes with a fixed order. The concurrency report includes setup and teardown, identifies tests
running while other workers are idle, and saves intervals under
`.pytest_cache/jj-stack-concurrency/`. Compare elapsed time as well as worker utilization: keeping
more workers busy need not finish the suite sooner.

Prefer focused fixtures, direct setup, and clear assertions. Avoid tests that primarily:

- pin presentation that is not a machine or recovery contract
- check only that a wrapper forwards arguments to a mocked helper
- restate private implementation details
- snapshot generated text when semantic assertions would suffice
- exercise only a trivial happy path while leaving failure handling untested

Test names should state the protected rule, not merely list setup details. A failure should be
understandable from the name and assertions without reconstructing the entire fixture.

Code-size limits live in [`complexity-budget.toml`](../../complexity-budget.toml); increases follow
the root [complexity policy](../../AGENTS.md#complexity-control). More tests do not compensate for
an unnecessarily complicated design.
