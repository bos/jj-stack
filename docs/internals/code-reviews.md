# Code review guidelines

Use this guide for reviews of code, tests, and documentation in this repository.

## What a review should find

Prioritize issues that could cause:

- lost work or mutation of the wrong pull request or branch
- surprising user-visible behavior
- violations of the invariants in [design.md](design.md)
- broken or unclear recovery after a partial failure
- unnecessary complexity or poor component boundaries
- meaningful performance regressions

Do not spend the review budget on compatibility scaffolding. The project is under active
development and has no legacy formats or behavior to preserve.

## Start with the product rules

Read [design.md](design.md) and the repository [AGENTS.md](../../AGENTS.md) before reviewing a
behavior change. A change can be internally consistent and still be too complicated, surprising,
or hard for a `jj` user to understand.

Pay particular attention to interactions among the local `jj` DAG, remote refs, GitHub, and local
tracking. Common failures include:

- an interrupted command leaving some mutations applied and others pending
- tracking that no longer agrees with `jj`, a remote ref, or GitHub
- recovery that cannot reach a safe state on a rerun
- unusual DAG shapes after rewrites, relinks, local deletion, or divergence
- a stack-scoped command being affected by unrelated history
- cleanup deleting an artifact that another review still needs

## Keep fixes simple

Before asking for another guard, saved field, or recovery path, check whether the mechanism that
creates the troublesome state can be removed or simplified. Use these questions:

1. Can an ordinary supported workflow, observed failure, or documented platform behavior reach
   this state? If not, do not add code or a test for it.
2. Is an existing rule merely missing at one call site? Share that rule instead of creating a
   variant.
3. Would deleting the proposed or existing mechanism also delete the failure mode?
4. Does a persisted field have one owner, one representation, and a clear deletion rule? New
   durable state requires a design change, not a local defensive patch.
5. Does every fail-closed error give a concrete next step when recovery is possible?
6. Is this the third consecutive hardening change in the same subsystem? If so, revisit the
   design instead of adding another patch.
7. Does a replacement remove the old path in the same change?

Match safeguards to the harm they prevent. The priority order is lost commits, mutation of the
wrong PR or ref, guessed linkage, then inconsistencies in reconstructible metadata. Do not build
an elaborate recovery system to protect data that can be observed again.

The limits in `complexity-budget.toml` are design constraints. Review changes to the limits,
governed paths, and test markers as carefully as production code. CI runs
`uv run tools/check_complexity.py`; run it locally when the pinned `tokei` version is installed.
Moving the same logic into a helper or neighboring module is not a reduction in complexity.

## Review the user experience

Assume the user knows `jj`, Git, and GitHub, but not this tool's implementation.

Treat docs, help, diagnostics, and ordinary output as part of correctness. Check for:

- an unclear explanation of what happened or what to do next
- implementation terminology in user-facing text
- wording that overstates destructive behavior
- noisy output or inconsistent behavior across similar commands
- disagreement among the code, help, and documentation

Internal docs should also use plain English. Project-specific terms are useful only when they
name a real type or enduring rule and are defined where they first appear.

## Check performance

Flag work that adds visible latency or scales poorly with repository size, including:

- history-wide scans where a bounded query would work
- repeated `jj` or GitHub calls that could be batched or run concurrently
- serial network requests with no ordering dependency
- algorithms that grow poorly with stack or repository size

Account for `jj` process startup and GitHub latency, not just in-process cost.

## Check maintainability

Look for:

- dead or nearly dead code
- duplicate non-trivial logic
- policy in adapters or rendering code
- vague names or modules that contain only one small forwarding layer
- validation that has no demonstrated user need

Prefer precise types in domain APIs. Dynamic types and casts are sometimes necessary at argparse,
async-protocol, or untrusted-JSON boundaries; they should be narrowed immediately. Flag `Any`,
`object`, `cast`, or `getattr` when they leak into domain logic or conceal a missing model.

## Review tests by risk

Follow [testing-philosophy.md](testing-philosophy.md). Give extra scrutiny to changes involving:

- states produced by supported commands or documented external actions
- configuration lookup failures, invalid values, or settings inconsistent with the repository
- unusual DAG topology and stack selection
- consistency among `jj`, remote refs, GitHub, and local tracking
- interrupted operations and recovery

Require coverage for a distinct, plausible failure at the narrowest useful layer. Do not ask for
large matrices, exact request-order assertions, or speculative race schedules without an observed
trigger or documented platform contract.
