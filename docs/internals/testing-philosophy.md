# Testing philosophy

When adding, removing, or evaluating tests, optimize for tests that protect real failures. That
usually means tests that cover off-happy-path behavior, not just the clean success case.

## What makes a test worthwhile

A test is worthwhile only if it protects at least one of:

- a user-visible behavior that would matter if broken
- a hard constraint from `jj`, GitHub, subprocess execution, or local persistence
- a realistic regression or failure mode
- a broken or surprising state the tool must handle safely
- a core invariant listed in [AGENTS.md](../../AGENTS.md)

Do not treat repo-authored docs, comments, or existing tests as sufficient justification by
themselves. They are hints, not proof that something is worth testing.

Before adding a test, identify:

- what regression it would catch
- why that regression matters in practice
- why this is the right layer to test it

If you cannot answer those clearly, do not add the test.

## Bias toward off-happy-path coverage

For this repo, do not assume the happy path is the main risk. Much of the real risk comes from
broken environments, drift between systems, and stateful operations that interleave in
surprising ways.

When reviewing coverage, ask first:

- what happens if the repo is already in a bad state?
- what happens if config is missing, invalid, contradictory, or only partially applied?
- what happens if `jj-stack`, `jj`, GitHub, local persistence, or subprocess state disagree?
- what happens if the DAG shape is unusual because of rewrites, relinks, divergence, non-linear
  history, or deleted changes?
- what happens if an operation is interrupted, retried, or followed by another command before
  the world is consistent again?

Prefer tests that show the tool fails closed, preserves work, and gives the user a recovery
path.

Examples of high-value test scenarios:

- broken or unexpected repo state
- bad config, missing config, or config that conflicts with actual state
- surprising DAG topology or stack selection edge cases
- interrupted operations and partial cleanup
- stale, missing, or contradictory tracking state
- drift or surprising interleaving across `jj-stack`, `jj`, GitHub, and
  subprocess-visible state
- recovery paths after a command discovers inconsistent state

## Apply backpressure to speculative coverage

Do not add a test just because something could go wrong in theory. A failure-mode test is
worthwhile when the scenario is both plausible and important.

Before adding an off-happy-path test, ask:

- is this state reachable in real use, through supported commands, manual user actions, partial
  failure, or normal tool drift?
- if it happens, could it lose work, leave cross-system state inconsistent, block recovery, or
  confuse the user badly?
- do we want a defined safe behavior here, rather than treating it as an internal bug where
  crashing is acceptable?
- can we cover the risk with one targeted test rather than a large matrix?

If the answer to those questions is mostly no, do not add the test.

Examples that usually do not deserve dedicated tests:

- purely imaginary states with no believable path from real usage
- large cross-product matrices where one representative case proves the rule
- internal corruption cases where the product does not promise graceful recovery and a crash is
  acceptable
- third-party failures we do not handle and cannot usefully recover from

The point is not to test every bad thing that could happen. The point is to
test the bad things that are plausible and costly.

## Choosing the right layer

Prefer tests at the narrowest layer that still exercises meaningful behavior.
Prefer one strong behavior test over many shallow plumbing tests.

Unit tests are much cheaper than integration tests, so when a unit test can protect the same
real risk, prefer the unit test.

Pick the layer that best protects the real risk:

- use integration tests for behavior that depends on the `jj`/GitHub/persistence boundary,
  unusual repo state, or cross-system interleaving
- use unit tests for nontrivial domain logic and failure handling
- keep CLI smoke coverage, but do not exhaustively test parser forwarding or presentation glue

## Keep the suite fast

The test suite is a product asset too. Keep it fast.

Prefer:

- the narrowest layer that still proves the risk
- one strong hostile-state test over many near-duplicates
- focused fixtures and direct setup over expensive end-to-end setup when the higher layer adds
  no extra confidence
- representative cases that prove a rule, not exhaustive combinations

Do not pay for broad scenario matrices unless each added case protects a meaningfully different
failure mode.

## Low-value test patterns

Avoid tests that primarily:

- pin exact wording, formatting, headings, or help output
- assert that a thin wrapper forwards arguments to a mocked helper
- restate private implementation details
- duplicate coverage already provided at a more meaningful layer
- cover only the happy path when the real risk is failure handling or drift
- snapshot generated text or scripts when only general behavior matters

## Reviewing existing unit tests

When reviewing an existing unit test, ask:

- what real regression would this catch?
- is unit level still the narrowest layer that matches the real risk?
- does the test assert a meaningful outcome, or only an internal branch,
  helper call, or forwarded argument?
- would a failure be easy to understand from the test name and assertions?

If you cannot answer those clearly, the test should usually be renamed, moved
up a layer, or deleted.

Test names should explain the rule being protected, not just the setup.

Prefer names like:

- `test_cleanup_skips_stack_comment_lookup_when_open_pr_still_has_remote_branch`
- `test_status_reports_divergent_stack_with_targeted_jj_guidance`

Avoid names that only enumerate setup details without stating the policy or
reason the behavior matters.

## Deleting and consolidating tests

Bias toward fewer, higher-signal tests by removing or consolidating low-value, speculative, or
redundant coverage. Do not cut tests that protect plausible failure paths, recovery behavior, or
important cross-system invariants.
