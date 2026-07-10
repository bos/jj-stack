# Property-based stack testing

`jj-stack` has a distributed state boundary: the local `jj` DAG, local review
tracking, remote Git branches, and GitHub PR state can temporarily disagree. The most
expensive failures are not wrong text output; they are cases where a normal stack edit
causes GitHub to close, merge, replace, or misbase an existing review. Property-based
testing should spend its budget on those cross-system invariants.

## Requirements

- Test user-reachable stack edits: reorder, reparent, insert, abandon, rewrite,
  squash, split-stack suffix moves, two-stack merges, single-change moves between
  independently submitted stacks, and combinations of those edits after an initial
  successful submit.
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
- Include external-drift coverage driven by an explicit transition model. A separate
  scenario family perturbs the boundaries after initial submit — GitHub PR state, remote
  branch state, saved tracking state, and the local `jj` view — using only transitions an
  ordinary user, teammate, or agent can perform. The model predicts whether `submit` must
  fail closed without mutating any boundary or succeed with the normal contract, and every
  drifted state must still produce a `view` report instead of a crash. See
  [distributed-state.md](distributed-state.md) for the state-holder model behind the
  vocabulary.
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
- inserting a new change above existing submitted ancestors
- abandoning a middle change
- rewriting a reviewed change without changing topology
- squashing one reviewed change into its predecessor

Random generation then fills the remaining budget with unique scenario representatives.

The supported successful-submit operations should cover the common linear-stack edit
surface:

- move an existing live change to the top of the current stack
- move an existing live change before or after another live change
- insert a new change after an existing live change, then rebase descendants onto it
- insert a new change before an existing live change
- abandon an existing submitted change while at least one live change remains
- rewrite an existing live change while preserving its `change_id`
- squash a live change into its predecessor

Those operations cover the common single-selected-stack failure classes while staying
small enough for quick shrinking by inspection. Broader operations such as multi-stack
merges, duplicate, split, and failed-submit injection can be added once their
expected product semantics are represented directly in the scenario model.

## Cross-Stack Split Harness

Some ordinary `jj rebase -s ... -d ...` edits split one submitted stack into two live
stacks. Those scenarios need a separate oracle because the successful-submit
invariant is no longer "every surviving submitted change is in the selected stack."

Cross-stack split scenarios start from one submitted linear stack, move a suffix onto an
earlier target so at least one submitted change is left behind on a deferred live stack,
then submit only the selected resulting stack. The oracle asserts:

- the selected resulting stack is rediscovered from the current DAG and submitted
  normally
- selected changes keep their PR numbers and approvals, and their PR bases and branch
  heads match the selected DAG
- deferred live-stack changes keep their saved local tracking record unchanged
- deferred PR branches still point at their originally submitted commits
- deferred PR bases, head branches, state, and approvals are unchanged
- fake GitHub recorded no base-retarget event for a deferred PR and no state transition
  for any original PR

The initial operation family is intentionally suffix moves because that is the common
linear-stack edit that produces two selected-parent chains without introducing merge
commits.

## Stack-Merge Harness

Merging two independently submitted linear stacks into one selected stack is a supported
cross-stack rewrite. The user has kept the same logical `jj` changes and moved them into
one review chain, so the expected behavior is to keep the existing PRs rather than
opening replacement reviews.

Stack-merge scenarios create two separate stacks from trunk, submit both, approve every
PR, rebase one stack root onto the other stack head, then submit the merged stack head.
The oracle asserts:

- every selected change from both original stacks keeps its PR number
- original approvals remain attached to those PR numbers
- every review branch points at the merged-stack commit for that `change_id`
- every PR base is recalculated from the merged selected DAG
- no PR is closed, merged, or replaced during the merge submit
- the merged stack has one selected-stack topology in tracking state

The initial scenario family covers both directions: appending the second stack after the
first and appending the first stack after the second, with small stack sizes plus random
size combinations.

## Stack-Move Harness

Moving one change between two independently submitted linear stacks is also a supported
cross-stack rewrite. The destination selected stack should adopt the moved change's
existing review, because the logical `jj` change is the same. The source-stack remainder
is a deferred live stack, so submitting the destination stack must not silently update its
PRs or saved local tracking.

Stack-move scenarios create two separate stacks from trunk, submit both, approve every
PR, then rebase exactly one source-stack revision before or after a target-stack
revision. The oracle submits only the destination stack head and asserts:

- every selected destination-stack change keeps its PR number
- the moved source-stack change keeps its PR number and approval
- selected PR bases and branch heads match the new destination DAG
- source-stack remainder PR branches still point at their originally submitted commits
- source-stack remainder PR bases, state, saved tracking, and approvals are unchanged
- no original PR is closed, merged, or replaced during the move submit
- fake GitHub recorded no base-retarget event for a deferred source-stack PR

The fixed scenario family covers moving a middle, head, bottom, and single-stack-source
change, with insertion before and after destination revisions. Random scenarios vary both
stack sizes, source direction, source index, target index, and insertion side.

## External-drift Harness

Stack-edit scenarios cover successful repair after supported local DAG rewrites. They do
not cover behavior when another state-holder has moved independently. The external-drift
family starts from a submitted, approved stack, optionally applies one local stack edit
from the stack-edit vocabulary, then applies one or two drift operations from a typed
transition vocabulary. Each drift kind is data: the boundary it mutates, whether it is
composable with other drifts, whether it targets one submitted change, and the modeled
`submit` outcome. [distributed-state.md](distributed-state.md) describes the state-holder
model and lists every drift kind with its expected outcome and recovery path.

Fail-closed kinds (for example an externally closed, merged, or replaced PR, a corrupted
saved PR number, an explicitly unlinked change, a drifted or deleted remote review branch,
or a foreign branch fetch that makes a stack change immutable or divergent) must produce a
contractual exit code and one of the kind's expected diagnoses while leaving every
boundary untouched: no remote ref changes, no local or remembered-remote bookmark changes,
no PR, review, or comment mutations, and byte-for-byte-equivalent saved tracking state.
That includes keeping a newly inserted change free of bookmark and tracking state when an
older submitted change makes preflight fail. The diagnosis is the typed
identity of the CLI's fail-closed error — a `DriftError` condition, an
`unsupported_stack:<reason>`, or `conflicted_stack` — captured from the error the CLI
hands its top-level printer, so a stop that fired for the wrong reason cannot pass on
exit code alone. Success kinds (external trunk advance, an externally retargeted PR base,
an external draft toggle) must converge on the full successful-submit contract.

Drift transitions stay faithful to the platform: deleting a remote review branch also
closes its PR because GitHub does, and a replacement PR created outside the tool shares
the original head branch. The generator composes drifts only in reachable combinations —
label-targeted drifts pick distinct live submitted changes, and shape-changing kinds
(conflicted rebase, merge commit, the recreated-change incident) stay in fixed scenarios.

Every drift scenario, fail-closed or successful, ends by running `view` on the drifted
selection and requiring a report exit (`0`, `2`, or `10`) rather than a crash or an
unclassified error. Exact diagnostic wording stays out of scope.

The fixed corpus includes one composite incident scenario, `agent-recreated-pr`: an agent
closes a reviewed PR, deletes its review branch, abandons the local change, recreates the
same work as a new change, pushes it with plain git, opens a replacement PR outside the
tool, and fetches. The fetched untracked remote bookmark makes the recreated change
immutable, so `submit` must refuse with the unsupported-stack diagnostic and `view` must
still report.

## Land Harness

Land scenarios compose the states `land` actually meets: a submitted, partially approved
stack that may have been edited since its last submit. Each scenario starts from a
submitted linear stack, optionally applies a short trace of stack edits from the shared
edit vocabulary — rewrite, insert, abandon, reorder, and squash, with or without a
follow-up resubmit — approves a prefix of the final live stack, then lands through one
transport. Scenario dimensions also cover `--pull-request` selection, which caps the
walk at the selected change, and a second independently submitted bystander stack that
the land must leave byte-for-byte untouched even though the trunk moves under it.

The walk model stays small because only two properties of an edited change can stop the
readiness walk: a change whose content no longer matches its remote review branch (a
rewrite target or a squash destination) is content-divergent, and an inserted change
without a resubmit has no pull request. Every other rebased change — moved, reordered,
or rebased past an abandon — is diff-equivalent, which land refreshes and lands. An
unapproved PR is the natural stopping boundary.

For the default direct-push transport, the oracle asserts:

- remote trunk points at the last landed local commit, and stays put when nothing is
  ready to land
- landed PRs are finalized as merged, and their remote review branches are left intact
  at the landed commits
- landed local review bookmarks are forgotten unless `--skip-cleanup` is present
- local review tracking for the landed prefix is retired; tracking above the landing
  boundary and for orphaned changes is untouched
- `list --json` stops reporting landed changes and still reports the remaining tracked
  suffix

For `land --via merge`, the oracle asserts the opposite tracking rule for landed PRs:
GitHub moves trunk by merging the PRs, local `jj` commits are left untouched, and the
merged tracking records remain so a follow-up `sync` or `cleanup --rebase` can rebase the
local stack. A blocked merge-transport scenario marks the first PR after the merged prefix
as unmergeable; the command must stop there, preserve the blocker as open tracking, and
keep merged tracking for the prefix that GitHub already accepted.

Both transports assert transient events, not only final state: a landed PR transitions to
closed exactly once, and no other original PR sees any state or base event. The one
exception is the first blocked merge-transport PR, which may be retargeted to trunk
before GitHub refuses the merge but must never change state.

## Land Drift Harness

Land drift scenarios apply one external transition to a submitted, fully approved stack,
then run `land` on its default selection so the drifted state must survive the
in-command fetch. The model predicts one of three outcomes:

- fail closed: an externally advanced trunk, or a bottom PR squash-merged outside the
  tool, must stop `land` with a classified error before any mutation — no PR events, no
  remote ref changes, and unchanged saved review identity
- prefix stop: an externally closed PR, a draft toggle, a changes-requested review, or a
  deleted mid-stack review branch stops the readiness walk at the drifted change, and
  the prefix below lands normally with the standard direct-push contract
- fetch abandons: deleting the head change's review branch lets the fetch abandon the
  local change (nothing else references it), so the re-resolved selection lands the
  untouched survivors below it

The mid-stack versus head split for deleted branches mirrors jj's own semantics: a
mid-stack change stays visible because descendants' bookmarks keep it reachable, while
an unreferenced head is abandoned by the fetch. Every drift scenario ends by running
`view` on the default selection and requiring a report exit rather than a crash.
In both prefix-stop and fetch-abandon outcomes, the stopping change keeps its durable
bookmark, PR, and submitted-topology identity plus its exact GitHub PR state; `land`
owns only the prefix it actually landed and leaves that recovery evidence for explicit
follow-up. Managed stack-comment IDs may clear as the landed prefix is finalized.

## Land Retry Harness

Land retry scenarios interrupt one direct-push land at a checkpoint, then rerun it and
require convergence rather than rollback. The fault family covers a remote trunk push
whose success acknowledgement is lost before the applied event reaches the operation
log, a failed load of the first landed PR after the push is durably recorded, a failure
on the second landed PR after the first finalized, and a dropped completed marker after
tracking retirement — the crash window between retiring the landed records and sealing
the operation log.

The oracle spans both runs with one event window: each landed PR transitions to closed
exactly once in total, so the rerun provably finalizes only what the interrupted run
left unfinished. The rerun must end with the standard direct-push contract, a completed
land operation as the journal's last land event, and `list --json` free of the landed
prefix. Merge-transport interruption is deliberately absent here: GitHub has already
moved trunk for the accepted prefix, so its recovery path is the sync/cleanup handoff
chain, not a naive rerun.

## Land Handoff Harness

Merge-transport land and external merges both leave documented multi-command recovery
work behind, and the handoff family replays that contract end to end. A prefix reaches
trunk through GitHub merges — `land --via merge` stopped by an unapproved change, the
same land interrupted mid-merge by a fault, or squash merges performed outside the tool
with GitHub's usual head-branch auto-delete. Then `sync` (or `cleanup --rebase` plus
`submit`) rebuilds the suffix, and a final direct-push land consumes it.

The oracle asserts the recovery converged before the final land: every suffix change
keeps its PR number, bookmark, and pre-handoff approvals, the bottom suffix PR targets
trunk, review branches point at the rebased commits, and the merged prefix sees no
further event of any kind after the handoff begins. For external merges, the recovery's
rebase pass must itself retire the pre-merge local copies — they are provably the
reviewed commits — while merge-transport lands leave copies pinned immutable by their
untracked review branches, so a closing `cleanup` retires those; either way the chain
must end with `list --json` empty and no tracking for any original change.

## Interrupted-Submit Retry Harness

Boundary-drift scenarios assert that unsafe external state blocks mutation. Retry
scenarios cover the opposite case: `submit` has already performed some intended
mutation, then a later operation fails. The expected behavior is not rollback; it is a
safe rerun that discovers the partial artifacts and converges on the same final review
state without duplicate PRs or lost metadata.

Interrupted-submit scenarios create a fresh stack, install a one-shot failure at one
mutation checkpoint, run `submit`, leave the active submit recovery record in place, then
rerun `submit`. The oracle asserts:

- every selected change has exactly one PR after retry
- remote review branches point at the selected `jj` commits
- PR heads, bases, titles, and saved topology match the selected DAG
- configured labels and reviewers converge even if the first run failed during metadata
  sync
- an existing reviewed PR keeps its PR number and approval when the failed run was a PR
  update rather than the first submit

The initial failure family covers after remote branch push, after PR creation, after PR
update, and after PR metadata label sync. Later retry families can add stack-comment
failures, draft-state mutations, review rerequest mutations, and failures interleaved
with external GitHub changes.

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
`--seed <int>`, `--cross-stack-scenarios <N>`, `--stack-merge-scenarios <N>`,
`--stack-move-scenarios <N>`, `--retry-scenarios <N>`, `--drift-scenarios <N>`,
`--land-scenarios <N>`, `--land-drift-scenarios <N>`, `--land-retry-scenarios <N>`,
`--land-handoff-scenarios <N>`, `--jobs <N|auto>`, `--no-sync`, and additional pytest
arguments after `--`.

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

- `JJ_STACK_SUBMIT_PROPERTY_SCENARIOS`: target number of unique generated scenarios
- `JJ_STACK_SUBMIT_PROPERTY_CROSS_STACK_SCENARIOS`: target number of unique cross-stack
  split scenarios
- `JJ_STACK_SUBMIT_PROPERTY_STACK_MERGE_SCENARIOS`: target number of unique two-stack
  merge scenarios
- `JJ_STACK_SUBMIT_PROPERTY_STACK_MOVE_SCENARIOS`: target number of unique cross-stack
  single-change move scenarios
- `JJ_STACK_SUBMIT_PROPERTY_RETRY_SCENARIOS`: target number of unique failed-submit
  retry scenarios
- `JJ_STACK_SUBMIT_PROPERTY_DRIFT_SCENARIOS`: target number of unique external-drift
  scenarios
- `JJ_STACK_SUBMIT_PROPERTY_SEED`: deterministic random seed
- `JJ_STACK_LAND_PROPERTY_SCENARIOS`: target number of unique land scenarios
- `JJ_STACK_LAND_DRIFT_PROPERTY_SCENARIOS`: target number of unique land drift
  scenarios
- `JJ_STACK_LAND_RETRY_PROPERTY_SCENARIOS`: target number of unique interrupted-land
  retry scenarios
- `JJ_STACK_LAND_HANDOFF_PROPERTY_SCENARIOS`: target number of unique merged-prefix
  handoff scenarios
- `JJ_STACK_LAND_PROPERTY_SEED`: deterministic random seed for all land scenario
  families

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
