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

- `docs/internals/design.md`, the single canonical product specification
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

## Guard against complexity spirals

This repo's worst historical failure mode was not a bug; it was a feedback loop: a
defensive patch created states only the defense could produce, those states needed their
own guards, the spec canonized each guard, and tests made every mechanism look
load-bearing. Reviews are the main brake on that loop. Apply these tests to every finding
and every fix — including your own proposed fixes:

- **The self-inflicted-state test.** For any new guard, saved field, or recovery path,
  ask: can the state it handles arise in a world without our machinery? If it exists only
  because of a mechanism we added, the finding is against the mechanism, not the missing
  guard. Sharpest form: would deleting the mechanism also delete the failure mode?
- **Rule-completion over rule-addition.** When a site is broken, first look for an
  existing rule elsewhere in the codebase that the site failed to apply, and apply that
  one, shared. A finding that proposes a new variant of an existing predicate or guard is
  itself a defect in the finding.
- **New durable state is a spec event, not a bugfix.** Any persisted field, phase enum,
  or ordering dependency between durable writes requires a spec amendment in the same
  change, plus answers to: what deletes this state, and what happens if that deletion
  never runs? If recovering the new state would need another mechanism, reject it.
- **Defenses require an observed trigger.** A reproduction, a live-API observation, or a
  user report. "The other system might do X" earns a backlog entry naming the experiment
  that would confirm it — never code.
- **Match guard strength to the cost hierarchy.** Name what a proposed guard actually
  protects, against the ranked kernel: lost commits > mutating the wrong PR or ref >
  guessed linkage > metadata consistency. Reconstructible state gets report-and-continue
  or repair-on-retry behavior, not exact-match validation; a guard stronger than its tier is
  complexity to delete, not rigor. Every fail-closed stop must name a runnable next
  step — "fix it manually" with no command means the design is incomplete.
- **Rate of hardening is itself a finding.** If the change under review is the third or
  later consecutive hardening of the same area, the correct review output is "stop
  patching; re-derive the theory," escalated as a design question — not approval of one
  more locally defensible fix.
- **Replacement includes deletion.** A change that introduces a new state, authority, or
  recovery mechanism without deleting the one it supersedes is unfinished. Do not accept
  "cleanup later" as complexity credit.
- **One jj-stack-owned durable policy fact has one authority and one representation.** Sharing
  low-level observation or persistence is useful; preserving two policy-bearing paths is not. If
  both old and new paths can decide or mutate the same fact, require the change to choose one.

The checked-in limits in `complexity-budget.toml` are a measurable design stop. Reviewers must
compare production, test, and total `scc` code-line counts, Ruff `C901` findings, and
recovery-module size with those limits. A budget breach is a design finding. Moving the same
policy into a helper, wrapper, or neighboring package does not count as simplification. CI runs
`uv run tools/check_complexity.py`; run it locally when `scc` is available. Edits to
`complexity-budget.toml`, the governed path list, or either pytest budget marker require the same
scrutiny as an implementation change. The gate caps marked tests; reviewers remain responsible
for marking every case in the bounded merge-and-recovery corpus.

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
