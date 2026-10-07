# Code review guidelines

Use this guide when reviewing code, tests, or documentation. Prioritize lost work, mutation of the
wrong PR or branch, surprising behavior, failed recovery, unnecessary complexity, and significant
performance regressions.

## Establish the intended behavior

Read [design.md](design.md) and the root [AGENTS.md](../../AGENTS.md) before reviewing a behavior
change. Trace the affected workflow through local commits, remote refs, GitHub, and tracking.
Check what happens if it stops partway through and the user follows the recovery instructions.

Pay attention to rewrites, relinks, divergence, and local deletion; these can leave the systems in
different states. Verify that unrelated history does not block a selected stack and that cleanup
does not remove something another PR or local path still needs.

Distinguish supported compatibility from speculative scaffolding. Supported tracking migrations
live in [`state/migrations.py`](../../src/jj_stack/state/migrations.py), and public JSON has a
[schema](../json-output.schema.json). Preserve supported interfaces or make an intentional,
documented compatibility change. Do not preserve abandoned internal mechanisms.

## Keep fixes simple

Before requesting another guard, saved field, or recovery path, ask:

1. Can a supported workflow, observed failure, or documented platform behavior reach this state?
   If not, do not add code or a test for it.
2. Is one call site missing an existing rule? Share the rule instead of adding a variant.
3. Could removing or simplifying a mechanism also remove the failure mode?
4. Does a persisted field have one owner, one representation, and a clear deletion rule? New
   durable state requires a design change.
5. Does a refusal give a concrete next step when recovery is possible?

Apply the root [complexity policy](../../AGENTS.md#complexity-control), including removing
replaced mechanisms in the same change and reconsidering a subsystem after repeated hardening.
Moving logic into another helper does not reduce its complexity. Review budget and governed-path
changes as carefully as production code.

Match safeguards to the harm they prevent. Protect commits and PR identity before reconstructible
metadata. An elaborate recovery system is rarely justified for data that can be observed again.

## Review the user experience and docs

Assume readers know `jj`, Git, and GitHub. Check docs, help, diagnostics, and output for:

- disagreement with supported behavior or with each other
- missing context about what happened, what changed, or what to do next
- wording that overstates guarantees or destructive effects
- implementation jargon, repeated explanations, or inconsistent names
- examples that omit prerequisites, run in the wrong repo, or cannot be followed as written

Internal docs need plain language too. Keep each rule in its owning document, define necessary
project terms, and replace metaphors with concrete checks and effects. Do not retain a statement
just because it sounds consistent with the surrounding prose; verify it against its source.

## Check performance and maintainability

Flag avoidable history-wide scans, repeated subprocess or API calls, and serial network requests
with no dependency. Account for `jj` process startup and network latency, not just Python work.
Check how queries and algorithms grow with stack and repo size.

Look for dead code, duplicated logic, policy in adapters or rendering, vague names, and forwarding
layers that add no useful separation. Validation should serve a demonstrated need.

`just dead-code` lists unreferenced functions, classes, and variables, and `just dead-code src`
also lists production code that only tests use. It matches by name, so a method that shares its
name with a used one looks live. `just dead-code-probe` takes a few minutes and finds what name
matching misses: it renames each field and method, drops each parameter default, and drops each
`| None` in a scratch copy, then reports the ones the type checker does not notice. Both report
candidates to check, not proof. Removing dead code can leave more behind, so repeat until both
report nothing new. Add names that frameworks call to `[tool.vulture]` in `pyproject.toml`; the
probe skips them too.

Flag wide signatures and parameters that functions only pass along. A value forwarded unchanged
through several calls belongs in the object that already carries the run's other fixed values,
and parameters that always travel together belong in one value. `just api-width` lists the
widest signatures, forwarding chains, recurring parameter groups, and arguments that never vary.

Use precise types in domain APIs. Dynamic types and casts can be necessary at argument parsing,
async protocols, or untrusted-JSON boundaries; narrow them there. Flag `Any`, `object`, `cast`, or
`getattr` when they hide a missing model or spread into domain logic.

## Review tests by risk

Follow [testing-philosophy.md](testing-philosophy.md) and, for generated cases,
[property-testing.md](property-testing.md). Identify the distinct failure each case protects and
check for overlapping coverage. Require the narrowest layer that demonstrates the risk, including
an integration case when the bug depends on jj DAG behavior or interactions between systems.

Avoid large matrices, private request-order assertions, and speculative race schedules. Ordering
assertions are warranted when they protect an actual safety requirement, such as validating all
selected identities before changing the first PR.
