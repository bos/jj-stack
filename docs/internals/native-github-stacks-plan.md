# Native GitHub stacks implementation plan

Status: temporary implementation authority

This file is the single implementation authority for native GitHub stack support while that work
is underway. It keeps the accepted behavior, external evidence, architecture, delivery order, and
remaining questions together so a long implementation does not depend on conversation history.

## Progress

Commits 1 through 12 are complete. Commit 13, historical native resource members, is next.
Update this section in the same change that completes each remaining implementation commit.

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

Observed on 2026-07-23 and 2026-07-24:

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
- native landing submits `PUT /repos/{owner}/{repo}/pulls/{target}/merge-async` with one optional
  `merge_method` field and polls
  `GET /repos/{owner}/{repo}/pulls/{target}/merge-async/{uuid}`
- the target PR selects the contiguous unmerged bottom prefix through that PR; targeting the
  bottom of a two-PR resource landed only the bottom PR even though the upper PR was also ready
- a draft target was rejected synchronously with `400` and no mutation
- an accepted request returned `202`, `status: pending`, a UUID, the resolved merge method, and
  the target PR's current head as `expected_head_sha`
- the request accepts the ordinary `sha` field as a caller-controlled expected head; a false SHA
  returned `400` before mutation, while the correct SHA was accepted
- an `expected_head_sha` request field is ignored; that name is response data, not the guard
- `sha` guards only the target PR head, not every lower PR boundary: after the lower branch was
  advanced to an existing commit in the unchanged target's ancestry, the request was accepted and
  landed with the new PR boundary
- moving the lower branch to a commit outside the target's ancestry instead reached terminal
  `failed` state because the stack needed to be rebased, with no mutation
- polling reached either `status: merged` with the final trunk SHA or `status: failed` with a
  reason
- a concurrent identical request returned `409` with the existing UUID, merge method, and
  expected head, but not the target identity needed to adopt that operation safely
- repeating a completed successful request returned `200`, `status: merged`, and the same final
  trunk SHA
- a selected prefix with a merge conflict reached terminal `failed` state with trunk, both PRs,
  both heads and bases, and native resource membership unchanged
- after a partial squash landing, the native resource retained both the merged PR and the open
  survivor; GitHub retargeted the survivor to trunk and rebased its branch to a new commit with
  the same tree
- merge-commit landing of two PRs created one merge commit for the group and reported that same
  commit as both PRs' `merge_commit_sha`
- squash and rebase landing of two one-commit PRs each created two rewritten linear commits; each
  PR reported its corresponding landed result and the terminal response reported the top commit
- a completely landed native resource remained readable with all historical members and
  `open: false`
- with a temporary merge-queue ruleset, async stack merge was accepted for processing but
  reached terminal `failed` state because changes had to use the queue; no repository state
  changed
- ordinary `gh pr merge` could not enqueue the native member either: GitHub required sequential
  landing through the stack merge API
- directly fast-forwarding trunk to the bottom submitted commit made GitHub mark that PR merged
  with the exact pushed commit as its merge result; the native resource remained, while survivor
  heads and chained bases were unchanged
- directly fast-forwarding trunk across the next two active PR commits did not mark either PR
  merged, even though both exact commits were now on trunk
- in a fresh resource, directly pushing the first bottom PR marked it merged after a short delay;
  pushing the next exact active-bottom commit after that historical prefix existed left the PR
  open because its base remained the historical review branch
- unstacking that partially landed resource removed both active members and returned the
  historical merged prefix as the remaining closed resource
- attempting to unstack a fully historical resource returned `422` because merged members cannot
  be removed

The admission experiments used a third PR plus temporary branch protection and merge-queue
rulesets. The landing experiments used additional disposable resources and a separate temporary
merge-queue ruleset. Every ruleset and protection was removed afterward, and repository
auto-merge was disabled again.

The repository remains disposable. Its original `#1 -> #2` resource and the later lower-boundary
resource `#27 -> #28` are fully merged. Intentionally failed resources include `#17 -> #18` and
`#24 -> #25`; direct-push resources retain open PRs that demonstrate the non-compositional result.

### Local gh-stack implementation

The default-branch source at `~/dev/gh-stack` establishes the closest upstream precedent for
membership. Open upstream PR `github/gh-stack#307`, reviewed at
`a14ba2a49502b358e2247f8d36afba18e834241c`, proposes the corresponding landing client:

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
- the proposed landing client uses the same async submit and poll routes observed live
- its submit request contains only `merge_method`; it omits the live-confirmed `sha` guard and is
  therefore not a mutation-safety precedent for jj-stack
- it selects the target PR from a contiguous open, non-draft bottom prefix and otherwise leaves
  checks, reviews, conflicts, and repository rules to the mutation
- it performs a separate GraphQL merge-queue preflight, but the live endpoint already returns a
  terminal rule failure, so this is not a required jj-stack roundtrip
- its REST wrapper discards the useful `409` response body; jj-stack decodes it for diagnostics
  but does not adopt the UUID because the body does not identify the target PR
- the PR is unmerged precedent, not authority; its atomicity and partial-landing claims were
  independently confirmed in the disposable repository

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
- selected `sync` when saved reviews have terminal landed evidence or unexplained reviewed-branch
  drift that may be a native historical-prefix recovery
- `sync --all` before mutating any exact-on-trunk or terminal landed candidate

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

There is no automatic or explicit capability redetection. A dry-run may probe for an accurate
plan when the value is absent but never writes the result.

Sync resolves that one repository capability and reuses one stack list for every candidate before
mutating any of them. Selected sync uses the historical prefix and active suffix to recover an
interrupted native landing even when the landed bottom has left local ancestry; the native land
path passes its already-read membership directly into the same recovery. An uncertain detection
or membership read fails the command; it never treats unknown membership as legacy.

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
- closed and merged states are live lifecycle facts, not reasons to reject an otherwise valid
  local path
- topology rewrites remain valid after their resulting maximal reviewed paths are disjoint

GitHub also gives a native member one landing authority: it cannot independently have auto-merge
enabled or enter the merge queue. Apply that restriction in every repository:

- adopting or tracking the PR remains allowed because it does not compete for landing authority
- while either state is live, commands may inspect or repair identity but must not push its
  branch, update or close its PR, rewrite its reviewed history, move trunk through it, or race its
  landing
- observe these fields in the existing batched PR reads; do not add a capability request or a
  second preflight roundtrip
- report the live owner of the mutation and require the user to disable auto-merge or remove the
  PR from the queue before retrying

This restriction is about exclusive mutation authority, not topology. An unlinked local change
may still share history with such a PR, and a terminal merged PR is handled by ordinary
observational recovery.

Draft PRs are valid reviews and native members, but GitHub's native landing API categorically
rejects landing a draft. Apply that lifecycle boundary in every repository:

- `land` never includes a draft PR in its ready bottom prefix
- `--bypass-readiness` may bypass approval and changes-requested policy, but not draft state

This is the complete repository-independent restriction-convergence rule:

- GitHub's one-resource-per-PR rule changes jj-stack's review model in every repository
- GitHub's one-landing-authority rule makes queued and auto-merge-enabled reviews read-only until
  that delegation is removed, except for local identity inspection and repair
- GitHub's draft lifecycle makes draft reviews unlandable even when local readiness policy is
  bypassed
- restrictions on mutating a native resource govern the corresponding jj-stack operation
- limits of the native resource representation do not invalidate local review topology

In particular, jj-stack must never PATCH the base of a native member or merge one through the
ordinary PR merge API. It must use the native replacement and landing sequences. Native creation
requiring at least two PRs means a one-PR review has no native resource; it does not make that
review invalid. The append-only endpoint means replacement must dissolve and recreate a complete
resource; it does not make a local reorder invalid.

The single merge method per landing request already matches jj-stack's
one-method-per-command behavior, and the active bottom is the first member of its existing ready
bottom prefix.

Supporting native stacks of 100 or more reviews is explicitly out of scope. Submit sends the
complete desired membership in one create request, or all new top members in one append request.
Do not add request-size validation, batching, size-specific recovery, or tests for unusually large
stacks. A request GitHub refuses is an ordinary GitHub error.

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
- the live native active suffix already equals the desired membership and submission needs no PR
  base mutation, including a protective pre-push retarget

`create`

- no existing selected PR belongs to a native stack
- create the native stack after PR synchronization

`append`

- one live native stack's active suffix is an exact ordered prefix of the desired sequence
- no current native member needs a PR base mutation, including a protective pre-push retarget
- every remaining desired position is a new or currently unstacked PR above that prefix
- append only the final PR-number delta

`replace`

- the desired sequence cannot be reached through append, or a current native member needs a PR
  base mutation
- examples include reorder, insertion below the current top, dissolving a surviving one-member
  resource, and removing an unreviewed position before PR creation
- a protective pre-push base retarget also requires replacement even when membership is exact
- unstack the active suffix exactly owned by the selected reviews before branch or PR-base
  mutation, allowing only its historical merged prefix to remain
- create one native resource after PR synchronization when the final sequence has at least two
  PRs

### Resource-closed selection

Native submission may automatically mutate at most one complete resource owned by the selected
reviews. Every active member of an overlapping resource must resolve to an active same-repository
`ReviewIdentity` in the selected maximal local review path. A validated historical merged prefix
is retained remote evidence, not an active selected review.

Fail before mutation when selected PRs overlap more than one native resource or an overlapping
resource contains an unselected active or closed-unmerged member. Do not dissolve collateral
active reviews, choose one local path as canonical, or coordinate repeated submissions. The error
identifies the resource and points to `gh stack unstack <number>`; after explicit dissolution,
ordinary selected submissions can create each disjoint desired resource.

Tracked orphan members are not a collateral exception. Explicitly dissolve their resource before
submitting a new local topology or clean them up through the existing orphan workflow.

### Live member admission

GitHub's create and append mutations are the authority for admitting new members. They require
each PR being added to be open, whether draft or ready for review, not queued for merge, and to
have auto-merge disabled. Surface mutation rejection and do not fall back to comments.

The repository-independent landing-authority gate uses queue and auto-merge fields already in the
batched PR observation. It is not a native admission preflight: the create or append mutation
still authorizes admission atomically, and jj-stack adds no read for that purpose. PRs already in
the exact target resource are not being admitted again. An anomalous locked member is handled by
the server's membership and unstack responses rather than becoming local topology.
Independently, jj-stack's active-review discovery still requires a selected saved PR to be open;
changing that lifecycle is outside native admission.

After `replace` dissolves a resource, every desired PR is admitted again during recreation. A
queued or auto-merge member may instead prevent complete dissolution, which is handled by the
fresh post-unstack membership check below.

### Historical merged prefix

Native landing retains merged members in the original resource. They form a historical bottom
prefix; the remaining open members are its active suffix.

Enrich the stack response model with the already-returned PR state, `merged_at`, and head ref/SHA.
Validate that merged members are a bottom prefix, then compare submission's desired membership
with the active suffix:

- exact active membership is `none`
- an exact active prefix can `append`
- active reorder or removal is `replace`
- a fully merged resource does not overlap a later all-new review stack

For replacement, authorize the complete resource and require its active suffix to be exactly
selected. The unstack response may retain exactly the historical merged prefix; that is expected,
not an incomplete-unstack error. The selected active PRs must no longer belong to that resource
before their replacement resource is created.

A closed but unmerged member is not historical. It remains an unresolved resource member and
blocks replacement until the user repairs or explicitly unstacks it. Do not treat arbitrary
unselected members as ignorable merely because they are not open.

This is one membership model used by submission and landing recovery. Do not normalize successful
landing by dissolving and recreating survivor resources: GitHub deliberately retains the
historical prefix, and exact or append submission can operate on its active suffix.

### Fresh authorization

Immediately before unstacking:

- fetch the exact stack resource again
- require its membership to match the plan

Immediately before appending or creating:

- list native stacks once
- re-run resource-closed planning against that fresh complete membership
- require the action and affected resource to remain exactly the planned action and resource
- compare stable authorization facts such as resource number and ordered membership, not the
  entire response model after observational state and head fields are added

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
11. re-read native authorization facts and apply one complete `create` or `append`
12. synchronize the applicable comment kinds
13. run the existing unexpected-PR-closure verification

If unstacking returns remaining members because GitHub considers them locked, stop. Do not push
branches or update PR bases after an incomplete unstack.

If execution stops after a successful unstack, the next `submit` observes unstacked PRs and plans
creation when at least two remain. If execution stops after PR synchronization, creation, or
append, the next `submit` observes live membership and computes what remains.

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

`sync --all` remains repository-wide, but native landed recovery must use the terminal-only
finalization rule below.

## Landing

Native landing is a separate external contract, not an extension of the existing ordinary PR
merge loop.

### Async merge contract

Submit:

```text
PUT /repos/{owner}/{repo}/pulls/{target_pr}/merge-async
```

```json
{
  "merge_method": "merge | squash | rebase",
  "sha": "<exact target PR head>"
}
```

The target selects every unmerged member from the bottom through that PR. `sha` is the
caller-controlled freshness guard. The response's `expected_head_sha` is the accepted value; a
request field by that name is ignored.

An accepted request returns `202` and:

```json
{
  "status": "pending",
  "details": {
    "uuid": "...",
    "merge_method": "...",
    "expected_head_sha": "..."
  }
}
```

Poll:

```text
GET /repos/{owner}/{repo}/pulls/{target_pr}/merge-async/{uuid}
```

Terminal success is `status: merged` with `details.sha`; terminal rejection is `status: failed`
with `details.message`. A stale `sha` or draft target returns `400` before mutation. A concurrent
request returns `409` with the existing UUID, method, and accepted head, and a completed retry
returns `200` with the prior merge result.

Decode the response body on `409` and require its returned `expected_head_sha` and resolved
`merge_method` to match for a useful "already pending" diagnostic. Do not adopt or poll its UUID:
the body does not identify the target PR, so it cannot prove that the operation belongs to this
request. A later rerun of the same target and exact SHA recovers through the endpoint's terminal
`200` response and fresh repository observation. Do not copy the proposed gh-stack client's loss
of the diagnostic body.

The conflict and merge-queue experiments both failed atomically. Treat only terminal `merged` as
acceptance, then re-read trunk, PR state, heads, bases, and native membership before local
convergence. Do not derive success from the HTTP acceptance or from a poll transport failure.

### Native landing selection

Reuse the existing local exact-snapshot and ready-bottom-prefix planning, but execute only its
first active bottom PR per invocation. This is the only request shape whose complete reviewed
snapshot is protected by the endpoint's one `sha` guard. For native merge:

- the active bottom PR is the one async target
- exactly that one PR is planned for landing even when more of the ready bottom prefix is ready
- every active resource member, including unlanded survivors, must resolve in order to a selected
  review on the same maximal local path
- historical merged members may precede that selected active suffix
- selecting only a lower member while omitting an active survivor fails before mutation, because
  GitHub rebases every survivor and would otherwise mutate an unselected review

Resolve native support and membership before rendering dry-run output, so dry-run shows one
native bottom-PR landing rather than the legacy per-PR merge loop. Re-read the resource and all
selected active PR heads immediately before the request, then supply the bottom PR's exact
submitted SHA.

The resource-closure read protects collateral survivor mutation. The request's `sha` protects the
complete selected snapshot because the target is the active bottom PR. The endpoint has no
compare-and-swap guard for lower member heads, which is why jj-stack does not target a multi-PR
prefix. GitHub remains the authority for checks, reviews, conflicts, branch rules, and
merge-queue rejection; do not add a separate mergeability or queue-policy roundtrip.

`--bypass-readiness` may skip approval and changes-requested policy but never the draft boundary.
It does not bypass GitHub rules.

### Native `land --via merge`

Use the native stack merge API. Never fall back to the ordinary bottom-up PR merge loop for native
members.

One native request uses the resolved merge method for the active bottom PR. Re-running `land`
observes the new historical prefix and lands the next exact active bottom.

On terminal success, use each merged PR's `merge_commit_sha` as existing rewritten-result
evidence:

- merge-commit mode reports one shared group merge commit
- each PR reports the commit GitHub records as its landed result
- the terminal response SHA is the final top commit, not each member's result

GitHub retains merged members as a historical resource prefix. After a partial async landing it
also retargets and rebases every survivor, changing survivor head SHAs even when their trees are
unchanged. The terminal response does not identify those survivor outputs, and an immediate read
cannot distinguish GitHub's rewrite from an external push in the same interval. Do not mint
authority from temporal proximity.

Do not add tree-equivalence evidence or adopt GitHub's rewritten survivor commits as local truth.
After terminal success, retire the landed bottom and rebase selected local survivors onto observed
trunk, but do not update their remote branches or saved baselines. Preserve each observed survivor
head and print its exact existing `relink` repair command, followed by the selected `submit`
command to restore the local jj snapshots after explicit authorization. The same rule handles an
interrupted operation. Exit `1` after a successful bottom landing when survivor repair remains, so
scripts cannot mistake partial convergence for completion. With no survivor, native landing
converges normally and exits `0`.

### Native direct-push landing

Native direct-push landing is unsupported. Pushing the first active bottom commit eventually made
GitHub mark that PR merged, but the resource retained chained bases. A later exact push of the new
active bottom left its PR open, as did one push across two active members. Supporting only the
first push would strand the remaining resource in a state that the same transport cannot advance.

`land --via push` therefore fails before mutation when any selected PR is an active native member.
Legacy repositories retain the existing direct-push transport. Native landed recovery must still
require terminal merged PR state instead of using the legacy open-PR retarget-and-close finalizer,
because an external actor can directly push trunk even though jj-stack does not.

## Other commands

Do not add native membership to commands merely because the API exposes it, but do not let an
existing mutation damage a native resource.

- `view` and `list` continue to report local stacks and saved review state
- `checkout` continues to bootstrap from review branches and PR bases
- `unstack --local` remains local-only
- remote `unstack` must select the exact active suffix before closing its PRs; an unstack response
  may retain the historical merged prefix, but any unselected active member fails closed
- cleanup must not delete an active member's branch; a historical merged member does not block
  ordinary landed-branch cleanup merely because the resource retains its record
- selected `sync` and `sync --all` may finalize a native member only after GitHub reports that PR
  merged; an exact commit merely appearing on trunk must not trigger the legacy retarget-and-close
  path

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
- async merge submit and poll

The fake must reject PR updates containing `base` while the PR is stacked, even when the value is
unchanged. It must reject the ordinary PR merge endpoint for a native member.

Model only the landing facts used by jj-stack: exact target SHA, bottom-PR result identity,
historical merged prefix, survivor rewrite for async merge, atomic failure, diagnostic `409`, and
terminal retry recovery. Do not implement a general GitHub stack emulator.

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
- merged native members form one historical prefix while submit plans against the active suffix
- native async merge uses one guarded target request, does not adopt a `409` UUID, and recovers a
  completed retry through terminal observation
- terminal failure changes no repository, PR, branch, or membership state
- merge-commit, squash, and rebase results feed existing landed evidence
- partial async landing never overwrites a rewritten survivor and reports its explicit relink
  repair before resubmission, then exits `1`
- selected sync recognizes the historical prefix after an interrupted native landing and reports
  the same survivor repair
- native direct push fails before mutation for every active native member
- an external direct push cannot make sync retarget or close an open native member
- draft landing remains blocked under `--bypass-readiness`
- queued and auto-merge-enabled reviews fail before external or reviewed-history mutation in
  either repository mode, while local identity adoption remains available

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

### Commit 11: converge common GitHub lifecycle restrictions

- observe queue and auto-merge ownership in existing batched PR reads
- prevent external or reviewed-history mutation while another GitHub landing mechanism owns it
- continue to allow local identity adoption and repair
- make draft state a hard land boundary even with `--bypass-readiness`
- retain read-only inspection and observational recovery

Exit condition: native and legacy repositories enforce the same exclusive landing authority and
draft lifecycle without another roundtrip or persisted state.

### Commit 12: native submission mutation

- create or append the complete desired membership in one request
- re-read and authorize observed membership immediately before mutation
- recover interruption by replanning from live membership without additional state

Exit condition: one authorized request applies the complete desired membership, and interruption
recovery needs no persisted operation state.

### Commit 13: historical native resource members

- retain state, merged time, and head details from native stack responses
- validate one historical merged prefix and plan submit against its active suffix
- allow replacement unstack to retain exactly that historical prefix
- compare stable resource identity and membership rather than rich response-model equality
- update remote unstack and cleanup guards to distinguish active from historical members

Exit condition: partial landing can leave GitHub's historical resource intact while ordinary
submit, restructure, unstack, and cleanup operate on active reviews without collateral mutation.

### Commit 14: native-safe landed recovery

- resolve native membership for terminal or survivor-drift selected recovery and for global
  candidates before mutation
- make selected and global sync require terminal merged state for a native member
- retain exact-on-trunk recovery for legacy reviews
- retire a terminal native bottom and rebase local survivors without publishing them
- report explicit survivor relink and resubmission commands

Exit condition: native landed recovery cannot retarget, close, or overwrite an open member, and
local survivors remain recoverable through exact explicit commands.

### Commit 15: native merge landing

- add the guarded async submit and poll client operations
- implement native merge landing
- render one exact bottom-PR action instead of legacy per-PR mutations
- recover a lost submit response through a later terminal retry, never by adopting a `409` UUID
- feed terminal landed evidence into native-safe recovery without inferring survivor authorship
- add only the merge-landing tests justified by observed behavior

Exit condition: `land --via merge` uses the native stack API for one exact active bottom PR and
preserves exact survivor authorization for explicit recovery.

### Commit 16: permanent documentation and plan deletion

- reconcile the finished behavior into `design.md`
- retain the explicit exclusion of support for native stacks of 100 or more reviews
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
- historical merged resource prefixes do not block active-suffix submission or landed cleanup
- disjoint cross-stack rewrites require explicit dissolution only when an old native resource
  spans more than one desired path, then converge through ordinary selected submission
- landing behavior is evidence-backed and cannot accidentally use the ordinary PR merge API
- queued and auto-merge-enabled reviews are never raced, and drafts cannot land under a bypass
- no GitHub stack topology or operation phase is persisted
- the canonical docs and user guidance describe the finished behavior
- this file has been deleted
