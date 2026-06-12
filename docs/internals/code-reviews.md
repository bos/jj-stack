# Code review guidelines

Use this when reviewing `jj-stack` changes or when asking a subagent to do a code review pass.

## Primary goal

Optimize for finding:

- likely regressions
- user-visible surprises
- violations of core invariants
- gaps in test coverage for subtle logic
- bad layering
- unnecessary complexity

## Start from the spec and the actual project constraints

Before raising a finding, anchor yourself in:

- `docs/internals/design.md`
- repo invariants from `AGENTS.md`
- explicit product decisions already made in the thread

If a change is internally consistent but still seems overly complex, surprising, or hard for a jj
user to understand, flag that. Internal coherence is not enough.

Also remember current project reality:

- this repo is under heavy development
- there is no meaningful backwards-compatibility burden yet
- migration code, legacy shims, and speculative guardrails are not a virtue

## Focus on major recurring bug classes

Many bugs here come from interactions between loosely coupled systems. Pay extra attention to:

- interrupted operations that leave work half-applied or hard to recover
- mismatch or drift between `jj-stack` tracking state, `jj`, GitHub
- states where recovery paths fail and the user can no longer get back to something sane
- unusual DAG topology, including rewrites, relinks, local deletions, and non-linear history
- cases where only one selected stack should matter, but surrounding history can interfere
- non-happy-path interactions between commands or subsystems
- cleanup behavior that might delete or preserve the wrong artifacts

These are much higher-value than generic style concerns.

## Review the user experience directly

Assume the user knows jj, git, and GitHub, but is not a power user of them, or of this tool.

Flag behavior or wording that makes the tool harder to learn, less safe, or harder to recover
from. In particular, check whether a user could understand what happened and what to do next.

Treat docs, help text, and CLI output as part of correctness. Review them for:

- internal jargon that leaks implementation details
- wording that is technically true but hard to understand
- scary wording that overstates destructive behavior
- output that adds noise instead of clarity
- inconsistency across commands that should feel uniform

Prefer language that matches how a jj user thinks, not how the implementation is structured.

## Performance matters

Flag changes that may create user-visible latency, unnecessary subprocess overhead, or work that
scales poorly with repo size.

Examples:

- O(all history) scans
- operations that could be batched or run concurrently
- poor algorithmic choices
- failure to account for `jj` startup overhead in a large repo, or slow GitHub responses

Some past changes introduced multiple calls to `jj`, or to the GitHub REST API, when one batched
`jj` call or a single GraphQL query would have been much faster.

## Review code for product need and maintainability

Subtle behaviors should be documented in the code and should also show up in commit descriptions.

Pay attention to:

- dead code or variables
- "nearly dead code": small helper functions with just one caller
- duplicate non-trivial logic
- poor layering, or code being added to the wrong module
- obtuse function or variable naming
- modules being invented to just contain one or two things
- small validation or guardrail code that hardens behavior without a real user need

## Bad smells

Agents sometimes introduce sloppy practices:

- `Any` or `object` in a type signature, when a more specific type would be appropriate
- `cast(...)` or `getattr`: occasionally okay in the test suite, with a high bar; effectively
  *never* okay in the main `src` tree

## Testing

Use [testing-philosophy.md](testing-philosophy.md) as the guide for judging whether added or
missing tests are justified.

Pay extra attention when a change touches:

- broken repo state or recovery paths
- bad, missing, contradictory, or partially applied config
- unusual DAG topology or stack-selection edge cases
- consistency across `jj-stack`, `jj`, GitHub, local persistence, and
  subprocess-visible state
- interrupted operations or surprising command interleavings

In those areas, scrutinize the proposed test coverage closely.

## Respect the current stage of the product

Be wary of review comments that push for backwards-compatibility scaffolding or complexity
without a demonstrated need. If the simplest design fits the stated product goals, prefer it.
