# Native GitHub stacks implementation plan

Status: temporary implementation authority

This file is the single implementation authority for native GitHub stack support while that work
is underway. It keeps the accepted behavior, external evidence, architecture, delivery order, and
remaining questions together so a long implementation does not depend on conversation history.

For this work only, this file supersedes conflicting native-stack or stack-comment statements in
`design.md`, `implementation-strategy.md`, and `backlog.md`. Existing behavior outside this scope
continues to follow `design.md`.

This file is intentionally temporary. The final implementation slice must:

1. update `design.md` to describe the finished behavior
2. update `implementation-strategy.md` only where component, storage, or testing strategy changed
3. update user documentation, built-in help, and the bundled skill where needed
4. remove or replace superseded native-stack backlog items
5. delete this file in the same change

Do not retain this plan as implementation history. The `jj` commits are the history after the
finished behavior has moved into its permanent authorities.

## Objective

Use GitHub's native stack infrastructure in repositories where it is available, while retaining
navigation comments in repositories where it is not.

The implementation must preserve the existing product invariants:

- the local `jj` DAG is the source of stack topology and content truth
- `ReviewIdentity` remains the authority for the PR attached to a change
- `SubmittedBaseline` remains the exact reviewed snapshot
- GitHub stack membership is derived remote state, not local topology
- an active or prospectively reviewed change belongs to at most one maximal local review path
- ordinary commands affect only selected review identities
- ambiguous remote identity or membership fails closed
- interrupted operations recover by observing current state, not by replaying durable intent

## Explicit non-goals

- no repository migration workflow between navigation comments and native stacks
- no removal, rewriting, or reconciliation of old navigation comments when native support appears
- no persisted GitHub stack ID, number, membership, order, or parent relation
- no user-selectable native-versus-comment mode
- no tri-state capability model
- no fallback to comments after a native operation fails
- no periodic capability probe, timestamp, or time-to-live
- no generalized stack-projection backend or plugin interface
- no native-stack changes to `view`, `list`, `checkout`, `unstack`, or cleanup without a concrete
  behavior that requires them
- no speculative tree-equivalence or server-rebase recovery

## External evidence

### Disposable repository

Repository:
`https://github.com/voxel-ai/jj-stack-native-stacks-test`

Identity and ownership:

- GitHub account: `bos-voxel`
- organization: `voxel-ai`
- repository visibility: private

Observed on 2026-07-23:

- `GET /repos/voxel-ai/jj-stack-native-stacks-test/stacks` returned `200`
- an empty list from that endpoint represented support with no existing stacks
- `gh stack link 1 2` recognized an existing `#1 -> #2` native stack as up to date
- the pull-request filter returned the containing stack for `#1` and an empty list for an
  unrelated PR number
- `POST /stacks/{number}/unstack` returned `204` for an unlocked two-PR stack
- unstacking left both PRs open and preserved their base branches
- recreating the same ordered PR membership preserved both PR identities
- recreation allocated a new stack number, proving that stack numbers are not stable identity
- a PR `PATCH` containing its unchanged `base` was rejected with `422` because the PR belonged
  to a stack
- attempting to merge the bottom PR through the ordinary PR merge API was rejected because a
  native member must be merged through the stack merge API
- stack creation and append each rejected an otherwise valid PR with auto-merge enabled, leaving
  the existing stack unchanged
- stack creation and append each rejected an otherwise valid PR in the merge queue, leaving the
  existing stack unchanged
- enabling auto-merge on an existing native member was rejected because the stack merge API owns
  landing
- enqueueing an existing native member was rejected for the same reason, even when the stack's
  base branch had an active merge queue

The admission experiments used a third PR plus temporary branch protection and merge-queue
rulesets. The PR, rulesets, and protection were removed afterward, and repository auto-merge was
disabled again.

The disposable repository was left with an open native stack containing PRs `#1 -> #2`, both in
draft state.

### Local gh-stack implementation

The local source at `~/dev/gh-stack` establishes the closest upstream precedent:

- `link` is explicitly intended for external local managers such as jj and Sapling
- the stack-list endpoint returns `404` when native stacks are unavailable
- errors other than `404` are ordinary API failures
- stack creation accepts an ordered list of at least two PR numbers
- stack update appends only to the top
- exact membership is a no-op
- removal or reorder is not expressible through the append endpoint
- a PR may belong to only one native stack
- `link` refuses PRs spread across multiple resources or an update that would drop members
- `submit` restructures at most the one complete native resource represented by its local stack
- unstacking operates on the native stack resource and may leave queued or auto-merge PRs
  stacked
- `push` and `submit` also avoid pushing an anomalous queued member, but the live API currently
  rejects queueing or enabling auto-merge on an existing native member

The gh-stack `submit` command's treatment of every stack-list error as "unavailable" is not a
precedent for jj-stack. The `link` command's `404`-only handling is the relevant behavior.

## Capability cache

### Stored fact

The capability has exactly two values:

```text
stacked_pull_requests = true | false
```

An absent cache record means detection has not completed for that repository pair. It is a
storage condition, not a third capability value.

### Scope and representation

Store the value directly in jj-stack's machine-written local state, keyed by the resolved GitHub
repository:

```json
{
  "stacked_pull_requests": {
    "github.com/voxel-ai/example": true
  }
}
```

The enclosing state path identifies the local jj repository. The map key identifies the GitHub
host, owner, and repository resolved from its configured remote, so the complete cache key is the
local/GitHub repository pair without another API request.

Do not put the detected value in human-authored jj config. Do not create a second capability
store or a generalized capability record around the one boolean.

### Detection

Only a command whose behavior depends on native stack support consults the cache:

- every non-empty `submit`, because a single-PR submit may otherwise delete navigation comments
  left by a previously larger stack
- `land` and apply-mode remote `unstack` when they have selected saved PR identities
- cleanup only when it is about to delete a branch belonging to a known PR

When the repository pair has no cache entry:

1. call `GET /repos/{owner}/{repo}/stacks`
2. on `200`, cache `true` and reuse the returned stack list for this command
3. on a conclusive `404`, cache `false`
4. on any other response or transport failure, fail and write no capability value

A cached `false` uses navigation comments without a stack API request. A cached `true` does not
need another capability probe. A submit containing existing or retiring PR numbers reads current
membership for planning. An all-new submit cannot overlap an existing resource, so it waits for
the fresh membership read that authorizes creation instead of listing twice. Native mutations
still read current membership immediately before changing it.

There is no automatic or explicit capability redetection. `submit --dry-run` may probe for an
accurate plan when the value is absent but never writes the result.

If a required membership read returns `404` for a cached `true`, fail without changing the cached
value. Do not change implementations in the middle of a command.

## GitHub API boundary

Add typed client models and operations for only the observed endpoints:

- list repository stacks
- fetch one stack if a fresh single-resource check is needed
- create a stack from ordered PR numbers
- append ordered PR numbers to a stack
- unstack a stack

The client reports HTTP failures without deciding whether jj-stack should use native stacks or
comments. Capability selection and reconciliation policy remain in the command layer.

Use the server's human-facing stack number for resource URLs during one observed plan. Do not
persist the number or internal stack ID.

## Pull-request update separation

The current PR update sends `base`, `title`, and `body` together. Native GitHub stacks reject any
update containing `base`, even when the supplied value is unchanged.

Change the client and submit execution so a PR PATCH includes only changed fields:

- title/body refresh omits `base`
- a base change includes `base`
- an unstacked PR whose base and content both changed may receive all changed fields together

This is one PR update API with field-sensitive payload construction, not separate native and
legacy PR implementations.

Planning must classify base changes independently from title/body changes. A native member whose
desired base differs must be unstacked before the base mutation.

Update the permanent GitHub mutation-safety documentation in the final documentation commit to
describe the new field-sensitive behavior.

## Capability-independent review topology

GitHub's one-stack-per-PR rule is the jj-stack product model in every repository, not a native
submission special case:

- an active `ReviewIdentity`, or a change the current operation would attach to a PR, may appear
  in at most one maximal live root-to-head local review path
- non-empty working-copy heads, including working copies in other workspaces, are live path heads
- ownership is derived from the current `jj` DAG and is never persisted
- unlinked identities do not count as active reviews
- selected prefixes of one linear maximal path do not manufacture additional ownership

One discovery-layer authority validates this rule. It receives `ReviewState` plus the explicit
prospective changes for an operation and reports the shared changes and conflicting path heads.
Do not expose a caller-supplied list-of-paths policy helper in native submission.

Commands that create or adopt review identity validate the resulting prospective ownership before
bookmarks, state, GitHub, or remote branches change. Selected commands that mutate reviewed
history or PRs validate their connected component before mutation. Read-only inspection and
commands needed to repair the violation remain usable; an unrelated invalid component does not
block a selected operation elsewhere.

The rest of GitHub's restrictions do not become local topology:

- a one-change review remains valid even though GitHub creates no native resource for it
- append-only updates and base-update rejection are mutation sequencing
- closed, merged, queued, and auto-merge states are live admission or authorization facts, not
  reasons to reject an otherwise valid local path
- topology rewrites remain valid after their resulting maximal reviewed paths are disjoint

This is the complete restriction-convergence rule:

- GitHub's one-resource-per-PR rule changes jj-stack's review model in every repository
- restrictions on mutating a native resource govern the corresponding jj-stack operation
- limits of the native resource representation do not invalidate local review topology

In particular, jj-stack must never PATCH the base of a native member or merge one through the
ordinary PR merge API. It must use the native replacement and landing sequences. Native creation
requiring at least two PRs means a one-PR review has no native resource; it does not make that
review invalid. The append-only endpoint means replacement must dissolve and recreate a complete
resource; it does not make a local reorder invalid.

## Native submission planning

Plan the native operation after existing PR discovery and identity validation, but before local
bookmark movement, branch pushes, PR base updates, or PR creation.

The desired sequence is the selected local changes in bottom-to-top order. Existing changes
contribute their verified PR numbers. New changes occupy known positions and receive PR numbers
after PR creation.

The planner returns an executable action. Invalid or ambiguous state raises an error rather than
becoming a plan state.

When `submit --restart` retires saved PR identities, their old PR numbers participate in overlap
and resource-closure checks but never in desired membership. Restarting every member of a native
resource therefore plans replacement rather than mistaking the new PRs for an unrelated create.

### Actions

`none`

- no selected PR overlaps a native resource and the final stack has fewer than two PRs, or
- the live native membership already equals the desired membership and submission needs no PR
  base mutation, including a protective pre-push retarget

`create`

- no existing selected PR belongs to a native stack
- create the native stack after PR synchronization

`append`

- one live native stack is an exact ordered prefix of the desired sequence
- no current native member needs a PR base mutation, including a protective pre-push retarget
- every remaining desired position is a new or currently unstacked PR above that prefix
- append only the final PR-number delta

`replace`

- the desired sequence cannot be reached through append, or a current native member needs a PR
  base mutation
- examples include reorder, insertion below the current top, dissolving a surviving one-member
  resource, and removing an unreviewed position before PR creation
- a protective pre-push base retarget also requires replacement even when membership is exact
- unstack the one resource exactly owned by the selected reviews before branch or PR-base mutation
- create one native resource after PR synchronization when the final sequence has at least two
  PRs

### Resource-closed selection

Native submission may automatically mutate at most one complete resource owned by the selected
reviews. Every member of an overlapping resource must resolve to an active same-repository
`ReviewIdentity` in the selected maximal local review path.

Fail before mutation when selected PRs overlap more than one native resource or an overlapping
resource contains an unselected member. Do not dissolve collateral resources, choose one local
path as canonical, or coordinate repeated submissions. The error identifies the resource and
points to `gh stack unstack <number>`; after explicit dissolution, ordinary selected submissions
can create each disjoint desired resource.

Tracked orphan members are not a collateral exception. Explicitly dissolve their resource before
submitting a new local topology or clean them up through the existing orphan workflow.

### Live member admission

GitHub's create and append mutations are the authority for admitting new members. They require
each PR being added to be open, whether draft or ready for review, not queued for merge, and to
have auto-merge disabled. Surface mutation rejection and do not fall back to comments.

Do not duplicate this policy with a speculative PR-state preflight. Such a read would not
authorize the later mutation, which must still enforce the same facts atomically, and would add
another policy path and roundtrip. PRs already in the exact target resource are not being
admitted again. The live API rejects newly queueing or enabling auto-merge on a native member, so
jj-stack does not add speculative queue or auto-merge observation to ordinary native submission.
An anomalous locked member is handled by the server's membership and unstack responses rather
than becoming local topology. Independently, jj-stack's active-review discovery still requires a
selected saved PR to be open; changing that lifecycle is outside native admission.

After `replace` dissolves a resource, every desired PR is admitted again during recreation. A
queued or auto-merge member may instead prevent complete dissolution, which is handled by the
fresh post-unstack membership check below.

### Fresh authorization

Immediately before unstacking:

- fetch the exact stack resource again
- require its membership to match the plan

Immediately before appending or creating:

- list native stacks once
- re-run resource-closed planning against that fresh complete membership
- require the action and affected resource to remain exactly the planned action and resource

These are mutation-authorization reads, not capability probes.

## Native submission execution

The ordered live flow is:

1. prepare the selected local stack, descriptions, bookmarks, and desired PR data
2. validate capability-independent review-path ownership
3. load the GitHub repository and discover PRs
4. resolve cached native support, detecting and saving it only when absent
5. validate every saved and discovered review identity
6. load current native membership when support is enabled
7. derive the native action
8. for `replace`, re-read and unstack the selected complete native resource
9. apply safe local bookmark changes
10. run the existing protected branch-push and PR synchronization flow
11. re-read native authorization facts and apply `create` or `append`
12. synchronize the applicable comment kinds
13. run the existing unexpected-PR-closure verification

If unstacking returns remaining members because GitHub considers them locked, stop. Do not push
branches or update PR bases after an incomplete unstack.

If execution stops after a successful unstack, the next `submit` observes unstacked PRs and plans
creation when at least two remain. If execution stops after PR synchronization but before native
creation or append, the next `submit` observes the correct PRs and retries the remaining native
operation.

Do not persist an action, stack number, expected membership, retry phase, or operation journal.

## Navigation and overview comments

Navigation and overview comments represent different features and must no longer share one policy
decision.

Navigation comments:

- synchronize only when cached native support is `false`
- on a native repository, do not classify, reject, create, update, or delete them
- do not remove comments left by submissions made before native support was detected

Stack overview comments:

- continue to represent explicit or generated stack-level prose
- synchronize in both native and legacy repositories
- retain existing rules for one overview on the selected stack's head

Refactor the current combined comment synchronizer around those two concrete responsibilities.
Do not introduce a generalized projection interface.

Explicit cleanup commands may continue removing unambiguously managed comments as part of
ordinary review cleanup. That is not a repository-transition mechanism.

## Cross-stack rewrites

There is no repository-wide submission mode or automatic collateral reconciliation. The resulting
maximal local reviewed paths must first be disjoint. If one old native resource spans more than
one desired path, explicitly dissolve it with `gh stack unstack <number>`, then run ordinary
selected submission once for each desired stack.

A retry recomputes from the current local DAG and live membership. No command persists a
multi-stack plan.

`sync --all` remains unchanged in both repository types.

## Landing

Native landing is a separate external contract, not an extension of the existing ordinary PR
merge loop.

Live evidence already proves:

- an ordinary PR merge request for a native member is rejected
- PR base retargeting is rejected while the PR remains a native member

Before implementing native landing, determine:

- the stack merge endpoint and request schema
- how an expected head or equivalent freshness guard is supplied
- whether the endpoint lands only a ready bottom prefix or requires the complete stack
- how draft, review decision, checks, merge queue, and auto-merge affect eligibility
- whether a failed or partially accepted request can leave some PRs merged
- merge-result identity for merge, squash, and rebase modes
- resulting native membership and PR bases after partial landing
- whether direct trunk push changes or dissolves native membership

Record confirmed live behavior in this file while implementation is underway.

### Native `land --via merge`

Use the native stack merge API. Never fall back to the ordinary bottom-up PR merge loop for native
members.

Reuse existing `ReviewIdentity`, `SubmittedBaseline`, readiness, and expected-snapshot checks.
Extend merge-result evidence only where the observed stack merge response requires it. Do not add
tree-equivalence recovery merely because GitHub may rewrite commits.

Until the stack merge contract is implemented, both landing modes must detect native membership
and stop before mutation with a direct explanation. The guard must land before native submission
is enabled: direct-push landing can otherwise move trunk and then fail while finalizing a PR whose
base GitHub refuses to change.

### Native direct-push landing

The likely execution shape is:

1. authorize the complete selected native resource
2. unstack it before the trunk push and PR finalization
3. run the existing exact leased trunk push
4. finalize the landed PRs
5. recreate native membership for surviving selected reviews

This remains conditional until the live experiment confirms direct-push effects and survivor
behavior. If the contract requires a different design, update this section before implementing
it.

## Other commands

Do not add native membership to commands merely because the API exposes it, but do not let an
existing mutation damage a native resource.

- `view` and `list` continue to report local stacks and saved review state
- `checkout` continues to bootstrap from review branches and PR bases
- `unstack --local` remains local-only
- remote `unstack` must dissolve an exactly selected native resource before closing its PRs; if
  native membership includes an unselected PR, fail closed
- cleanup must not delete a branch whose PR remains in a native resource; it fails with the
  affected `gh stack unstack <number>` follow-up instead
- `sync` and `sync --all` retain their existing recovery behavior

Change one of these only when an implemented native behavior demonstrates a concrete correctness
or recovery requirement. Record that decision here first.

## Test strategy

Read and apply `testing-philosophy.md` before changing tests.

### Fake GitHub

Add only the observed stack endpoints:

- list stacks, including the pull-request filter if used
- get one stack
- create
- append
- unstack, including remaining locked members

The fake must reject PR updates containing `base` while the PR is stacked, even when the value is
unchanged. It must reject the ordinary PR merge endpoint for a native member.

Do not implement a general GitHub stack emulator. Add native landing behavior only after its live
contract is observed.

### Focused coverage

Add the narrowest tests protecting these distinct risks:

- capability detection caches `true` on `200`
- capability detection caches `false` on conclusive `404`
- an uncertain detection error writes no cache value
- a later invocation uses cached `false` without a stack API request
- a first successful native list is reused for submission planning
- native creation produces no navigation comments
- native submit may load an unfiltered issue-comment response for overview synchronization, but
  never classifies, rejects, updates, or deletes navigation comments
- native title/body refresh omits `base`
- exact membership is a no-op
- a prefix appends only the new top PRs
- reorder unstacking happens before any PR base mutation and preserves PR identity
- interruption after unstack recovers through ordinary resubmission
- active or prospective review ownership shared by maximal local paths fails before mutation
- ownership validation includes non-empty working-copy heads and ignores unrelated components
- submit, checkout, relink, and remote unstack enforce ownership at their mutation boundaries
- read-only inspection and local unstack remain available to diagnose or repair invalid topology
- multi-resource overlap and an unselected resource member both fail before mutation
- resource-closure errors name every affected resource and its exact `gh stack unstack <number>`
  command
- explicit stack overview prose still works in both repository types

Use a planner unit table for the action classification, one integration test per meaningful
cross-system risk, and one interruption/retry case. Do not duplicate the full submit property
corpus in both modes.

Add native landing coverage only after the corresponding live contract is recorded above.

## Implementation commits

Each commit is one bounded change with its tests and any temporary-plan update needed to describe
the resulting behavior. A guarded unsupported operation is acceptable between commits; an
operation that mutates partially and then discovers native incompatibility is not.

### Commit 1: simplify state comparisons

- consolidate the duplicated identity and baseline compare-and-write implementation
- preserve every existing conflict and malformed-record behavior
- create enough governed-code budget for the cache without increasing a limit

Exit condition: existing persistence tests pass with one comparison mechanism replacing the two
duplicated mechanisms.

### Commit 2: API and capability cache

- add the minimal typed stack membership models and observed client operations
- add the direct repository-pair boolean map and atomic store update
- add command-layer capability resolution
- keep the resolver unexposed until a command can use the result coherently
- add focused client and persistence coverage

Exit condition: capability resolution returns one boolean or raises, reuses its first live stack
list, and a fresh store instance can use cached `false` without a stack capability request.

### Commit 3: PR mutation separation

- make PR PATCH payloads field-sensitive
- separate base-change planning from content-change planning
- update existing callers and mutation-safety tests

Exit condition: title/body updates can succeed on an already native-stacked PR without sending
`base`, while legacy base corrections retain current behavior.

### Commit 4: native action planning

- add the pure `none`, `create`, `append`, and `replace` classification
- include protective pre-push retargets and surviving one-member resource remnants
- cover the decision table at the planner layer

Exit condition: every selected-stack membership shape has one action or one fail-closed error,
without network or persistence logic in the planner.

### Commit 5: native mutation safety gates

- make both landing modes fail closed on native membership pending their live contract
- prevent cleanup from deleting branches that still belong to a native resource
- make remote `unstack` either dissolve an exactly selected native resource first or fail closed
- add only the focused mutation-order tests for these existing commands

Exit condition: externally created native stacks cannot be damaged by an existing jj-stack
mutation, even before jj-stack starts creating native resources itself.

### Commit 6: unique reviewed-path ownership

- add one discovery-layer authority for active and prospective review ownership
- validate canonical maximal local paths, excluding unlinked identities
- apply it before review creation/adoption and selected review mutations in both repository modes
- remove the native-only shared-prefix helper and successful shared-sibling behavior it supersedes

Exit condition: an active or prospectively reviewed change belongs to at most one maximal local
review path, while read-only and repair commands remain usable.

### Commit 7: split comment responsibilities

- separate navigation synchronization from stack-overview synchronization
- preserve current legacy behavior at every call site
- add no projection interface or backend abstraction

Exit condition: later native submission can omit navigation work without changing overview or
legacy behavior.

### Commit 8: native create, no-op, and append submission

- use cached capability resolution in `submit`
- use native resources for create, exact no-op, and top-only append
- reject multi-resource overlap or an overlapping resource with an unselected member
- preserve old navigation comments without managing them on native repositories
- fail closed on `replace` until its ordered execution is implemented
- add focused fake-server and integration coverage

Exit condition: ordinary additive stack submission uses the correct repository implementation,
and structural edits stop before every local or remote mutation.

### Commit 9: resource-closed selected-stack replacement

- implement `replace` for one complete resource owned by the selected reviews
- unstack it before protected branch pushes or any PR base mutation
- dissolve a surviving one-member resource without creating a replacement resource
- add one interruption-and-retry integration case

Exit condition: ordinary supported single-stack edits preserve PR identity and recover
observationally after interruption.

### Commit 10: native landing evidence

- complete the approved live experiments
- record the resulting contract in this plan
- make no production mutation claim that the evidence does not support

Exit condition: endpoint, freshness, partial-result, and survivor behavior are concrete enough to
implement without speculative recovery.

### Commit 11: native merge landing

- implement native merge landing
- add only the merge-landing tests justified by observed behavior

Exit condition: `land --via merge` uses the native stack API with exact authorization and
observational recovery.

### Commit 12: native direct-push landing

- implement the observed safe direct-push sequence or retain a documented fail-closed rejection
- cover only the distinct direct-push mutation and survivor risks

Exit condition: every advertised landing mode either works on native stacks with exact
authorization and observational recovery or is explicitly documented as unsupported.

### Commit 13: permanent documentation and plan deletion

- reconcile the finished behavior into `design.md`
- update `implementation-strategy.md` for the actual final component and test boundaries
- update user docs, help, exit-code documentation, and the bundled skill where required
- remove or replace superseded backlog entries
- delete this file

Exit condition: the permanent docs describe the code as it exists, no normative decision remains
only in this plan, and the repository contains no stale implementation-plan artifact.

## Validation

For every code commit:

- run focused tests through `.venv/bin/python -m pytest` after `uv sync --locked`
- run `./check.py`
- run `uv run tools/check_complexity.py` when SLOCCount is available
- stop for design review rather than increasing a complexity budget

Before committing each code change:

1. request independent correctness and design/complexity reviews
2. address every material finding and rerun focused plus full validation
3. request a follow-up review of the amended diff
4. commit only after the follow-up review has no material findings

Docs-only changes under `docs/` do not require the code test suite.

## Completion definition

The work is complete only when:

- each local/GitHub repository pair has one cached boolean capability value
- repositories without native support retain the existing navigation experience
- repositories with native support use GitHub stacks and do not manage navigation comments
- explicit stack overview prose works in both repository types
- normal selected-stack edits create, append, or replace native resources safely
- disjoint cross-stack rewrites require explicit dissolution only when an old native resource
  spans more than one desired path, then converge through ordinary selected submission
- landing behavior is evidence-backed and cannot accidentally use the ordinary PR merge API
- no GitHub stack topology or operation phase is persisted
- the canonical docs and user guidance describe the finished behavior
- this file has been deleted
