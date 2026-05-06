# Property-based stack testing

`jj-review` has a distributed state boundary: the local `jj` DAG, local review
tracking, remote Git branches, and GitHub PR state can temporarily disagree. The most
expensive failures are not wrong text output; they are cases where a normal stack edit
causes GitHub to close, merge, replace, or misbase an existing review. Property-based
testing should spend its budget on those cross-system invariants.

## Requirements

- Test user-reachable stack edits: reorder, reparent, insert, abandon, and combinations
  of those edits after an initial successful submit.
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
- Include fail-closed boundary-drift coverage. A separate scenario family should perturb
  one boundary after initial submit, such as GitHub PR state, saved PR identity, or remote
  branch state, and assert that `submit` refuses to mutate unsafe state rather than
  opening replacement reviews.
- Make failures reproducible. Every generated scenario must have a stable name and a
  compact operation trace that can be copied into a deterministic regression test.
- Keep the default suite fast. Property scenarios are opt-in and must not run from the
  default `./check.py` pytest pass.
- Use all available workers when exploration is widened. The core harness should expose
  generated scenarios as ordinary data so pytest, a future CLI runner, or a long-running
  explorer can distribute them across cores.
- Skip duplicate states before test collection. If two generated operation traces produce
  the same final live stack, orphan set, hazard class, and rewritten-change set, keep one
  representative instead of replaying both through `jj` and fake GitHub.

## Integration Harness

The core harness should be runner-agnostic. It owns scenario generation, replay, fake
GitHub event inspection, and invariant checking as plain Python APIs. Pytest is only the
current execution adapter: it gives the opt-in runner temporary directories,
monkeypatching, captured output, concise assertion reporting, and `pytest-xdist`
scheduling.

The integration layer generates small `StackEditScenario` values. Each scenario has:

- an initial stack size
- an ordered list of stack-edit operations
- a stable scenario ID derived from the initial size and operation trace
- a canonical key based on the final live stack order plus abandoned submitted changes
- a hazard class, so de-duplication cannot accidentally remove every representative of
  a known risk class
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
8. Run `submit` again on the new stack head.
9. Assert the cross-system invariants.

The replay model must track stable `change_id`s for initial and inserted changes.
Subjects and filenames are only labels that make failure output readable.

The generated pool should start with a fixed corpus that always covers:

- moving the old bottom change
- moving a middle change
- inserting a new change below existing submitted descendants
- abandoning a middle change

Random generation then fills the remaining budget with unique scenario representatives.

The supported operations should start narrow:

- move an existing live change to the top of the current stack
- insert a new change after an existing live change, then rebase descendants onto it
- abandon an existing submitted change while at least one live change remains

Those operations cover the failure class that caused approval loss while staying small
enough for quick shrinking by inspection. Broader operations such as cross-stack moves,
multi-stack merges, and interrupted-submit injection can be added once the first harness
is stable.

## Boundary-drift Harness

Stack-edit scenarios cover successful repair after supported local DAG rewrites. They do
not cover fail-closed behavior when one external boundary is already inconsistent. A
second, smaller scenario family should start from a submitted stack, optionally apply a
local edit, perturb one boundary, and assert that `submit` fails without unsafe mutation.

Initial perturbations should stay representative rather than exhaustive. Start with:

- GitHub reports a saved PR head branch in a closed state
- saved tracking points a change at a different PR number than GitHub reports

Later perturbations can add cases such as:

- an existing remote review branch points at a different commit and the link is not
  proven by local state

The invariant is negative: no new PRs, no closed/reopened selected-stack PRs, no remote
branch updates for the protected stack, and a non-zero command result. Exact diagnostic
wording is out of scope.

## Invariants

For every live change after the final submit:

- local review state has a bookmark and PR number for the change
- if the change existed in the initial submitted stack, the PR number is unchanged
- the remote review branch points at the live `commit_id`
- the PR is open and unmerged
- the PR title still identifies the same local change subject
- the bottom PR targets the resolved trunk branch
- every other PR targets the previous live change's review branch
- saved `last_submitted_commit_id` matches the live `commit_id`
- saved `last_submitted_parent_change_id` matches the previous live `change_id`, or
  null for the bottom change
- saved `last_submitted_stack_head_change_id` matches the final live head `change_id`
- if the original PR had approval reviews, those reviews are still attached to the same
  PR number

For every abandoned submitted change:

- local review state still records the old bookmark and PR number
- the old remote review branch still points at the originally submitted commit
- the orphaned PR base is unchanged from the initial submit
- the PR is open and unmerged
- no surviving live change reuses the abandoned PR number
- original approval reviews are still attached to the orphaned PR

For the submitted stack as a whole:

- the number of PRs equals submitted live changes plus submitted orphaned changes
- a resubmit that succeeds never replaces an existing live PR with a new PR
- final PR bases are derived from the current `jj` DAG, not from saved topology
- fake GitHub recorded no close, merge, or reopen event for any originally submitted PR
- fake GitHub recorded no base-retarget event for orphaned PRs

## Efficiency

The harness should not rely on one large Hypothesis state-machine test for integration
coverage. A single stateful test cannot be split across `pytest-xdist` workers, and a
failure often minimizes to a request-order artifact rather than a user-level scenario.

Instead, the integration layer should generate a deterministic pool of candidate
scenarios and expose the unique representatives as data. The pytest adapter can
parameterize over that data, giving the opt-in runner all-core execution under
`pytest -n auto`.
A future CLI runner can shard the same scenario list without depending on pytest.

Property scenarios are launched by hand:

```console
$ tests/run_submit_property_scenarios.py 500
```

The runner accepts the scenario count as a positional argument. It also supports
`--seed <int>`, `--jobs <N|auto>`, `--no-sync`, and additional pytest arguments after
`--`.

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

The opt-in runner sets these environment variables for the pytest adapter:

- `JJ_REVIEW_SUBMIT_PROPERTY_SCENARIOS`: target number of unique generated scenarios
- `JJ_REVIEW_SUBMIT_PROPERTY_SEED`: deterministic random seed

Those variables configure the adapter; they are not part of the core harness contract.

## Relationship To Hypothesis

Hypothesis is still useful for pure model tests where examples are cheap and shrinking is
valuable. The integration harness is deliberately shaped differently: it prioritizes
parallel execution, deterministic scenario IDs, and canonical-state de-duplication. If a
pure transition model is added later, it should use the same scenario vocabulary and the
same invariants so counterexamples can replay through the integration harness.

## Promotion Rule

Randomized tests are a discovery mechanism, not the only guardrail. When a generated
scenario catches a bug, keep the property test and promote the minimized operation trace
into a deterministic integration test with a name that states the protected behavior.
