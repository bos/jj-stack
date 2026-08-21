# Testing philosophy

Tests should protect behavior or constraints that would matter if they broke. The goal is a small,
high-signal suite, not a catalog of every state the code can represent.

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

If another test would fail for the same reason, consolidate them. Parameter rows and fixed
generated scenarios count as separate cases. Fixtures, helpers, and generators are justified by
the useful cases they enable, not by their own implementation complexity.

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

Live GitHub checks are an opt-in release gate; they do not replace deterministic local coverage.
Record any known fake-server difference beside the affected fake behavior and test.

If a behavior has both component and integration risk, keep one representative integration test
and only the unit cases that protect additional decisions. CLI parsing tests are useful when
parsing, normalization, rejection, or selector precedence is the behavior at risk; aliases do not
need separate forwarding tests.

## Keep the suite useful

Prefer focused fixtures, direct setup, and clear assertions. Avoid tests that primarily:

- pin presentation that is not a machine or recovery contract
- prove that a wrapper forwards arguments to a mocked helper
- restate private implementation details
- snapshot generated text when semantic assertions would suffice
- exercise only a trivial happy path while the real risk is failure handling

Test names should state the protected rule, not merely list setup details. A failure should be
understandable from the name and assertions without reconstructing the entire fixture.

Checked-in complexity and test-count limits live in `complexity-budget.toml` and are enforced by
`tools/check_complexity.py`. A new test beyond a limit must replace overlapping coverage in the
same change. More tests do not compensate for an unnecessarily complicated design.
