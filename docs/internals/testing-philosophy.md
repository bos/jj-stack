# Testing philosophy

When changing or reviewing tests, optimize for coverage that protects real failures. That
usually means tests that cover off-happy-path behavior, not just the clean success case.

## Gate for every test change

A test is worthwhile only if it protects at least one of:

- a user-visible behavior that would matter if broken
- a hard constraint from `jj`, GitHub, subprocess execution, or local persistence
- a realistic regression or failure mode
- a broken or surprising state the tool must handle safely
- a core invariant listed in [AGENTS.md](../../AGENTS.md)

Do not treat repo-authored docs, comments, or existing tests as sufficient justification by
themselves. They are hints, not proof that something is worth testing.

Before adding or retaining a test case:

1. Name the user-reachable regression and its practical harm.
2. Search existing unit, integration, live, and property coverage. If an existing test would
   fail for the same bug, update or consolidate it instead of adding another case.
3. State the distinct failure the new case catches. Parameter rows and fixed generated scenarios
   count as separate cases.
4. Only then choose the cheapest layer that exposes that failure.

If you cannot answer those clearly, do not add or retain the test. A unit test is cheaper, not
automatically worthwhile. Do not preserve a low-value case merely by moving it down a layer.

Fixtures, helpers, and generators inherit their justification from the worthwhile cases they
enable. Test support code directly only when its failure could silently invalidate meaningful
coverage or make failures irreproducible.

## Bias toward off-happy-path coverage

For this repo, do not assume the happy path is the main risk. Much of the real risk comes from
broken environments, drift between systems, and ordinary but surprising sequences of stateful
operations.

When reviewing coverage, ask first:

- what happens if the repo is already in a bad state?
- what happens if config is missing, invalid, contradictory, or only partially applied?
- what happens if `jj-stack`, `jj`, GitHub, local persistence, or subprocess state disagree?
- what happens if the DAG shape is unusual because of rewrites, relinks, divergence, non-linear
  history, or deleted changes?
- what happens when one supported command or documented external action follows another before
  all systems agree on the result?

Prefer tests that show the tool fails closed, preserves work, and gives the user a recovery
path.

Examples of high-value test scenarios:

- broken or unexpected repo state
- bad config, missing config, or config that conflicts with actual state
- surprising DAG topology or stack selection edge cases
- interrupted operations and partial cleanup
- stale, missing, or contradictory tracking state
- drift or surprising command and external-action sequences across `jj-stack`, `jj`, GitHub, and
  subprocess-visible state
- recovery paths after a command discovers inconsistent state

## Apply backpressure to speculative coverage

Do not add a test just because something could go wrong in theory. A failure-mode test is
worthwhile when the scenario is both plausible and important.

Before adding an off-happy-path test, ask:

- is this state reachable in real use through ordinary sequences of supported commands,
  documented user actions, observed partial failures, or normal tool drift?
- if it happens, could it lose work, leave cross-system state inconsistent, block recovery, or
  confuse the user badly?
- do we want a defined safe behavior here, rather than treating it as an internal bug where
  crashing is acceptable?
- can we cover the risk with one targeted test rather than a large matrix?

If the answer to those questions is mostly no, do not add the test.

Examples that usually do not deserve dedicated tests:

- purely imaginary states with no believable path from real usage
- pathological configurations outside the product contract with no believable ordinary user path;
  documented rejections still deserve focused coverage when the CLI promises them
- instruction-level race schedules without an observed trigger or documented platform contract
- large cross-product matrices where one representative case proves the rule
- internal corruption cases where the product does not promise graceful recovery and a crash is
  acceptable
- third-party failures we do not handle and cannot usefully recover from

The point is not to test every bad thing that could happen. The point is to
test the bad things that are plausible and costly.

## Keep tests from ratcheting complexity

Tests can accidentally make an unnecessary mechanism look permanent. These rules keep coverage
focused on behavior users need rather than implementation history:

- **Tests pin contracts, not mechanisms.** Before asserting on internal state — saved
  records, phases, journal contents — restate the assertion as an outcome another system
  could observe: GitHub state, the jj DAG, an exit code plus its message. If it cannot be
  restated, the test is pinning a mechanism, and it will defend that mechanism against
  future deletion.
- **Prefer recovery outcomes over interruption matrices.** For interruptible operations, the
  high-value test interrupts the command, runs the documented recovery, and asserts the final
  state plus exactly-once external effects. One such property can replace many tests tied to
  individual interruption points. If every point needs different recovery, file a design bug
  rather than expanding the matrix.
- **Fixtures must be user-reachable.** Construct test states through
  supported commands, external-system mutations, or documented user actions. If a state
  can only be constructed by forging internal records no command writes, the scenario may
  not be real; the fixture is a smell pointing at either a phantom state or a missing
  command.
- **Deleting a guarantee deletes its tests, by policy.** When the spec removes a
  guarantee, the tests enshrining it are removed in the same change, and "this test would
  fail" is never an argument for keeping the guarantee.
- **Name the fake's idealizations.** Where the fake diverges from the real system,
  record the divergence explicitly so the affected behavior families are known to be
  untestable locally rather than silently green. Each named gap should point at a live
  check or a stricter fake mode that would establish the real contract.

Checked-in complexity and test-count budgets are defined in `complexity-budget.toml` and enforced
by `tools/check_complexity.py`. A proposed test beyond a budget must replace overlapping coverage
in the same change. More tests do not compensate for a mechanism whose reachable state space is
too large.

## Choosing the right layer

After a case passes the worthwhile-test gate, use the narrowest layer that exercises the behavior
at risk. This repo currently has two implemented layers and one planned boundary:

- **Unit/component:** parsing, planning, models, and one adapter with controlled collaborators.
  Temporary files and an in-process HTTP transport can still be unit-level.
- **Local integration:** the CLI against real `jj` and Git repositories and the fake GitHub
  server. Use this when confidence depends on revsets, DAG or workspace behavior, subprocesses,
  or cross-component state transitions.
- **Live (planned):** opt-in checks against real GitHub. Until this layer exists, uncertain
  external behavior needs a separately approved experiment and must remain conditional if that
  experiment is not run.

An unusual repo state does not automatically require integration coverage. Discovering or
constructing it through real `jj` belongs in integration; deciding what to do with an
already-modeled state belongs in unit/component coverage.

If behavior has both component and boundary risk, keep one representative integration test plus
only the unit cases that protect distinct decisions. Do not repeat the same matrix at both
layers.

CLI parsing tests are worthwhile when parsing, normalization, rejection, or selector ordering is
the behavior at risk. Fold simple aliases into an existing behavior test; do not add standalone
forwarding or help-output checks for every alias.

## Keep the suite fast

The test suite is a product asset too. Prefer focused fixtures, direct setup, and one strong
hostile-state representative over expensive end-to-end setup or exhaustive combinations. Do not
pay for broad scenario matrices unless each added case protects a meaningfully different failure
mode.

## Low-value test patterns

Avoid tests that primarily:

- pin exact presentation unless the exact form is required for machine consumption, command
  syntax, or safe recovery
- assert that a thin wrapper forwards arguments to a mocked helper
- restate private implementation details
- duplicate coverage already provided at a more meaningful layer
- cover only the happy path when the real risk is failure handling or drift
- snapshot generated text or scripts when only general behavior matters

## Reviewing existing tests

When reviewing an existing test, ask:

- what real regression would this catch?
- would another existing test already fail for the same bug?
- is this still the narrowest layer that matches the real risk?
- does the test assert a meaningful outcome, or only an internal branch,
  helper call, or forwarded argument?
- would a failure be easy to understand from the test name and assertions?

If you cannot answer those clearly, the test should usually be renamed, moved to the right layer,
consolidated, or deleted.

Test names should explain the rule being protected, not just the setup.

Prefer names like:

- `test_cleanup_skips_overview_comment_lookup_when_open_pr_still_has_remote_branch`
- `test_status_reports_divergent_stack_with_targeted_jj_guidance`

Avoid names that only enumerate setup details without stating the policy or
reason the behavior matters.

Bias toward fewer, higher-signal tests by removing or consolidating low-value, speculative, or
redundant coverage. Do not cut tests that protect plausible failure paths, recovery behavior, or
important cross-system invariants.
