# Property-based stack testing

`jj-stack` has a distributed state boundary: the local `jj` DAG, remote Git branches, and GitHub
PR state can temporarily disagree. Local tracking records what `jj-stack` previously did. The most
expensive failures are not wrong text output; they are cases where a normal stack edit
causes GitHub to close, merge, replace, or misbase an existing review. Property-based
testing should spend its budget on those cross-system invariants.

## Requirements

- Test user-reachable stack edits: reorder, reparent, insert, abandon, rewrite,
  squash, two-stack joins, single-change moves between independently submitted
  stacks, and combinations of those edits after an initial successful submit.
- Use real `jj` commands, real remote branch updates, the CLI entrypoint, and the fake
  GitHub server for integration coverage. A pure model may supplement this, but it must
  not replace replay through the actual integration boundary.
- Assert semantics, not presentation. Tests should check review identity, PR state,
  branch targets, and PR bases; they should not pin output wording or internal request
  ordering.
- Preserve review identity by `change_id`: a live submitted change keeps its existing
  PR, and a newly inserted change gets a new PR.
- Preserve review state: a successful resubmit must not accidentally close, merge, or
  replace selected-stack PRs. For previously approved PRs, preserving the same PR number
  is the approval-preservation property; the harness should approve the initial PRs and
  verify that surviving and orphaned original PRs still carry those review records.
- Preserve orphan semantics: an abandoned submitted change is removed from the live
  stack, but its open PR and remote branch remain intact for explicit cleanup.
- Catch transient damage, not only final state. The fake GitHub server should record PR
  state-transition events, and property tests should fail if a selected-stack PR ever
  transitions closed or merged during a successful resubmit.
- Include external-drift coverage driven by an explicit transition model. A separate scenario
  family applies one external or local drift after initial submit, using only transitions an
  ordinary user, teammate, or agent can perform. The model predicts whether `submit` must fail
  closed without mutating any boundary or succeed with the normal contract, and every drifted
  state must still produce a `view` report instead of a crash. See
  [distributed-state.md](distributed-state.md) for the sources-of-state model behind the
  vocabulary.
- Make failures reproducible. Every generated scenario must have a stable name and a
  compact operation trace that can be copied into a deterministic regression test.
- Keep the default suite fast. `./check.py` runs a fixed eight-case property corpus; larger
  generated or randomized pools remain opt-in.
- Use all available workers when exploration is widened. Scenario modules expose generated cases
  as ordinary data so pytest can distribute them across cores.
- Skip duplicate states before test collection. If two generated operation traces produce
  the same final live stack, orphan set, risk category, and rewritten-change set, keep one
  representative instead of replaying both through `jj` and fake GitHub.

## Where the code lives

- `tests/support/*_property_scenarios.py` defines models, generators, and fixed scenarios.
- `tests/support/*_property_harness.py` replays them through real `jj` and checks the results.
- `tests/property/*_property_scenarios.py` adapts the scenarios to pytest.
- `tests/run_submit_property_scenarios.py` launches larger opt-in runs.

## Integration harness

The scenario modules own generation and expected-result models. The harness modules replay cases,
inspect fake GitHub events, and check invariants through plain Python APIs. Pytest adapts those
APIs to temporary directories, monkeypatching, captured output, concise assertion reporting, and
`pytest-xdist` scheduling.

The scenario modules generate small `StackEditScenario` values. Submit uses one
`StackEditOperation` vocabulary and pure order-transition model with real-`jj` replay. Each
scenario has:

- an initial stack size
- an ordered list of stack-edit operations
- a stable scenario ID derived from the initial size and operation trace
- a canonical key based on the final live stack order plus abandoned submitted changes
- a risk category (`hazard_class` in the scenario data), so de-duplication cannot accidentally
  remove every representative of a known failure mode
- enough abstract state to distinguish equivalent-looking final stacks that require
  different remote mutation behavior, such as which changes were rewritten since their
  initial submit

Replay follows the same shape for every scenario:

1. Create a fresh fake GitHub repo and local `jj` repo.
2. Create the initial linear stack with labeled changes.
3. Capture each initial `change_id`.
4. Run `submit` once, establishing remote branches, PRs, and local tracking.
5. Approve every initial PR in fake GitHub.
6. Apply the scenario operations with real `jj` commands.
7. Rediscover the selected live stack from the current DAG and assert that its
   `change_id` order matches the scenario model. Subjects are diagnostics only.
8. Run `submit` again on the new stack head, allowing it to reconcile GitHub grouping.
9. Assert the cross-system invariants.

The replay model must track stable `change_id`s for initial and inserted changes.
Subjects and filenames are only labels that make failure output readable.

The default corpus keeps one fixed stack-edit representative: squashing a reviewed middle change
into its predecessor. It combines a rewritten destination with an orphaned reviewed identity.
Reorder, insertion, abandon, and plain rewrite syntax already have deterministic command coverage;
the generator still explores all of them when an opt-in count exceeds the fixed set.

The successful-submit operations cover the common linear-stack edit surface:

- move an existing live change to the top of the current stack
- move an existing live change before or after another live change
- insert a new change after an existing live change, then rebase descendants onto it
- insert a new change before an existing live change
- abandon an existing submitted change while at least one live change remains
- rewrite an existing live change while preserving its `change_id`
- squash a live change into its predecessor

Those operations cover the common single-selected-stack failure classes while staying
small enough for quick shrinking by inspection. Separate harness families cover two-stack
joins, single-change moves between stacks, and failed-submit retries. Duplicate is not
represented in the current model.

## Cross-stack harnesses

Two harness families cover edits that involve more than one submitted stack:

- **Join:** submit the combined maximal path directly. Reuse every PR and approval by
  `change_id`; recompute heads and bases; store no topology.
- **Move:** submit the nonempty source remainder first, then the destination path. Reuse the
  moved change's PR and refresh both resulting paths.

Both families assert that no original PR is unexpectedly closed, merged, or replaced. Selected PR
bases, branch targets, and submitted baselines are recomputed for both resulting paths. Fixed cases
cover joining two stacks and moving a middle change while leaving a nonempty source remainder.
Expanded runs vary directions, sizes, and insertion points.

Focused deterministic tests cover resubmitting after `jj split` creates an additional change and
the `view` warning after a reviewed path gains a sibling.

## External-drift harness

Stack-edit scenarios cover successful repair after supported local DAG rewrites. They do
not cover behavior when another source of state has moved independently. The external-drift
family starts from a submitted, approved stack, optionally applies one local stack edit from the
stack-edit vocabulary, then applies one drift operation from a typed transition vocabulary. Each
drift kind is data: the boundary it mutates, whether it targets one submitted change, and the
modeled `submit` outcome.
[distributed-state.md](distributed-state.md) owns the drift inventory and expected outcomes.

Fail-closed kinds (for example an externally closed, merged, or replaced PR, a drifted or deleted
remote review branch, or a foreign branch fetch that makes a stack change immutable or divergent)
must produce a contractual exit code and one of the kind's expected diagnoses without mutating
remote refs, the local DAG, PRs, reviews, comments, or tracking.
That includes keeping a newly inserted change free of remote review branches and tracking when
an older submitted change makes preflight fail. The structured diagnosis comes from the CLI's
fail-closed error: a `DriftError` condition or `unsupported_stack:<reason>` captured from the
error handed to the top-level printer. A stop that fired for the wrong reason cannot pass on exit
code alone. Each drift kind owns explicit allowed `(exit code, diagnosis)` pairs. Success kinds
(external trunk advance, an externally retargeted PR base, an external draft toggle) must reach
the full successful-submit result.

Drift transitions stay faithful to the platform: deleting a remote review branch also
closes its PR because GitHub does, and a replacement PR created outside the tool shares
the original head branch. Conflict and merge-commit boundaries are covered by focused
deterministic command tests.

Every drift scenario, fail-closed or successful, ends by running `view` on the drifted
selection and requiring a report exit (`0`, `2`, or `10`) rather than a crash or an
unclassified error. Exact diagnostic wording stays out of scope.

## Merge and sync coverage

Merge and post-merge convergence use focused deterministic integration tests and a bounded
lifecycle family rather than the stack-edit generator. The three fixed lifecycle cases cover
cleanup, restart, and automatic sync after a partial rebase merge. Expanded runs sample the
finite direct-merge grid: stack sizes one through five, every nonempty bottom prefix, and rebase
or squash. A whole-stack squash merge is the first expanded representative. Focused tests cover
exact submitted-head validation, bottom-prefix selection, draft and closed boundaries, atomic
stack-merge failure, terminal retry, historical-member cleanup, and selected or repository-wide
sync eligibility.

The lifecycle cases assert final Git and PR state and the significant merge request. A
terminal stack-merge failure changes nothing. A successful partial request may change survivor
heads and bases. A terminal direct `merge` validates and converges that transition before
returning, including when a middle pull request names the merge boundary but higher survivors
were not named by the user. Queue acceptance leaves local history alone, and a remote-success /
local-failure case proves the outcomes remain distinguishable. These are bounded command
contracts, not generated merge property families or a durable recovery state machine.

## Interrupted-submit retry harness

Boundary-drift scenarios assert that unsafe external state blocks mutation. Retry
scenarios cover the opposite case: `submit` has already performed some intended
mutation, then a later operation fails. The expected behavior is not rollback; it is a
safe rerun that discovers partial work and reaches the same final review
state without duplicate PRs or lost metadata.

Interrupted-submit scenarios create a fresh stack, install a one-shot failure at one mutation
point, run `submit`, then follow the supported retry path for that fault. The fixed case retries
after the branch push succeeded but a later step failed; opt-in generation also explores PR
creation, update, and metadata failures. The checks require:

- every selected change has exactly one PR after retry
- remote review branches point at the selected `jj` commits
- PR heads, bases, and titles match the selected DAG
- configured labels and reviewers match the requested state even if the first run failed during
  metadata sync
- an existing reviewed PR keeps its PR number and approval when the failed run was a PR
  update rather than the first submit

The failure family covers failures after a remote branch push, PR creation, PR update, or label
sync.

## Invariants

For every live change after the final submit:

- `ReviewIdentity` records the saved repository, PR number, and canonical head owner/ref for the
  change
- if the change existed in the initial submitted stack, the PR number is unchanged
- the remote review branch points at the live `commit_id`
- the PR is open and unmerged
- the PR title still identifies the same local change subject
- the bottom PR targets the resolved trunk branch
- every other PR targets the previous live change's review branch
- the distinct `SubmittedBaseline` record matches the live `commit_id`
- if the original PR had approval reviews, those reviews are still attached to the same
  PR number

For every abandoned submitted change:

- local review state still records the old remote branch and PR number
- the old remote review branch still points at the originally submitted commit
- the orphaned PR base is unchanged from the initial submit
- the PR is open and unmerged
- no surviving live change reuses the abandoned PR number
- original approval reviews are still attached to the orphaned PR

For the submitted stack as a whole:

- the number of PRs equals submitted live changes plus submitted orphaned changes
- a resubmit that succeeds never replaces an existing live PR with a new PR
- final PR bases are derived from the current `jj` DAG
- fake GitHub recorded no close, merge, or reopen event for any originally submitted PR
- fake GitHub recorded no base-retarget event for orphaned PRs

The default suite runs eight fixed scenarios: five stack-edit, cross-stack, retry, and drift
representatives plus three completed-command lifecycle cases. Their names and counts live in
`tests/support/submit_property_scenarios.py`; larger deterministic pools remain opt-in.

## Efficiency

The harness does not rely on one large state-machine test for integration coverage. A single
stateful test cannot be split across `pytest-xdist` workers, and a failure often minimizes to a
request-order artifact rather than a user-level scenario.

Instead, the scenario modules generate a deterministic pool of candidates and expose the unique
representatives as data. The pytest adapter parameterizes over that data, giving expanded runs
all-core execution under `pytest -n auto`.

Expanded property runs are launched by hand:

```console
$ tests/run_submit_property_scenarios.py 500
```

The runner's `--help` lists family counts, seeds, workers, environment setup, and
additional pytest arguments. Pytest arguments must follow a literal `--`; unknown arguments
before it are runner errors.

`--random-seed` generates one seed and uses it for both scenario generation and
pytest-randomly ordering. The runner prints a complete reproduction invocation with the
resolved seed, every family count, worker count, sync choice, and extra pytest arguments.
GitHub CI uses this mode with a randomized seed and bounded counts for every family, so a failing
CI log contains the exact command needed to replay the same scenario pool and test order locally.

The generator defaults should remain modest for quick local runner invocations. Runner
configuration supplies:

- target number of unique generated scenarios
- deterministic random seed

The generator should cap stack size and trace length. When it cannot find enough unique
scenarios within a bounded number of attempts, it should return the unique scenarios it
found rather than looping indefinitely.

Collection under `pytest-xdist` must be deterministic on every worker, and a non-pytest
runner should see the same scenario order. The generator therefore uses a fixed seed,
stable sorting, no Python hash-order dependence, and concrete caps for stack size, trace
length, and attempts. Each replay receives an explicit workspace directory and fake repo
builder from the caller.

The runner configures the pytest adapter through internal `JJ_STACK_*` environment variables.
Those variables are implementation details, not part of the harness contract.

## Why this is not a Hypothesis state machine

State-machine tools can still be useful for pure model tests where examples are cheap and
shrinking is valuable. The integration harness is deliberately shaped differently: it
prioritizes parallel execution, deterministic scenario IDs, and canonical-state de-duplication.
The existing pure transition model uses the shared operation vocabulary and invariants so its
scenarios replay through the integration harness.

## Promotion rule

Randomized tests are a discovery mechanism, not the only guardrail. When a generated scenario
catches a bug, retain the minimized trace in the fixed corpus, or add a focused deterministic
case only if it protects a distinct boundary. Consolidate overlapping coverage and stay within
the fixed-case and SLOC budgets.
