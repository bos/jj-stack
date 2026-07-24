# Native GitHub stacks implementation plan

Status: temporary implementation authority

This file is the single implementation authority for native GitHub stack support while that work
is underway. It keeps the accepted behavior, external evidence, architecture, delivery order, and
remaining questions together so a long implementation does not depend on conversation history.

## Progress

Commits 1 through 14 are complete and their step descriptions have been pruned. Commit 15,
native merge synchronization, is next.

The implementation list contains only unfinished slices. In the same change that completes a
slice:

1. move any lasting behavior into the normative sections above
2. remove that slice's implementation subsection
3. remove or consolidate tests and evidence that exist only to guide the completed slice
4. update this section to name the next unfinished slice

Do not let this file become a changelog. Completed implementation belongs in `jj` history.

For this work only, this file supersedes conflicting native-stack, stack-comment, and merge
lifecycle statements in `design.md`, `implementation-strategy.md`, and `backlog.md`. Replacing
`land` with `merge` and removing direct trunk pushes apply in both repository modes. Existing
behavior outside this scope continues to follow `design.md`.

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

Merging is always a GitHub operation. A selected native resource uses GitHub's atomic stack merge.
A repository without native support, or a one-PR review with no native resource, uses GitHub's
ordinary PR merge API. jj-stack never advances trunk by pushing to it.

## Explicit non-goals

- no repository migration workflow between navigation comments and native stacks
- no removal, rewriting, or reconciliation of old navigation comments when native support appears
- no persisted GitHub stack ID, number, membership, order, or parent relation
- no user-selectable native-versus-comment mode
- no tri-state capability model
- no fallback to comments after a native operation fails
- no fallback from a failed native merge to the ordinary PR merge API
- no periodic capability probe, timestamp, or time-to-live
- no generalized stack-projection backend or plugin interface
- no native-stack changes to `view`, `list`, `checkout`, `unstack`, or cleanup without a concrete
  behavior that requires them
- no speculative tree-equivalence or server-rebase recovery
- no direct-push merge transport or `--via` transport choice
- no compatibility alias from the old `land` command
- no one-PR-at-a-time native merge loop
- no post-merge relink protocol for GitHub-rewritten native survivors

## External evidence

### Disposable repository

Repository:
`https://github.com/voxel-ai/jj-stack-native-stacks-test`

Observed on 2026-07-23 and 2026-07-24:

- attempting to merge the bottom PR through the ordinary PR merge API was rejected because a
  native member must be merged through the stack merge API
- native merge submits `PUT /repos/{owner}/{repo}/pulls/{target}/merge-async` with one optional
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
- direct trunk pushes were non-compositional: GitHub sometimes marked the first bottom PR merged,
  but later exact pushes left active PRs open while their chained bases remained unchanged
- unstacking that partially landed resource removed both active members and returned the
  historical merged prefix as the remaining closed resource
- attempting to unstack a fully historical resource returned `422` because merged members cannot
  be removed

The merge experiments used disposable resources and a temporary merge-queue ruleset. Every
ruleset and protection was removed afterward.

The repository remains disposable. Its original `#1 -> #2` resource and the later lower-boundary
resource `#27 -> #28` are fully merged. Intentionally failed resources include `#17 -> #18` and
`#24 -> #25`; direct-push resources retain open PRs that demonstrate the non-compositional result.

### Local gh-stack implementation

Open upstream PR `github/gh-stack#307`, fetched in `~/dev/gh-stack` and reviewed at
`a14ba2a49502b358e2247f8d36afba18e834241c`, proposes the corresponding `merge` command:

- the proposed merge command uses the same async submit and poll routes observed live
- its submit request contains only `merge_method`; it omits the live-confirmed `sha` guard and is
  therefore not a mutation-safety precedent for jj-stack
- it skips a historical merged prefix, then offers the contiguous open, non-draft bottom prefix
- an explicit PR chooses any bottom-anchored prefix through that PR; non-interactive operation
  chooses the highest candidate and therefore the complete candidate prefix
- it otherwise leaves checks, reviews, conflicts, and repository rules to the mutation
- it performs a separate GraphQL merge-queue preflight, but the live endpoint already returns a
  terminal rule failure, so this is not a required jj-stack roundtrip
- its REST wrapper discards the useful `409` response body; jj-stack decodes it for diagnostics
  but does not adopt the UUID because the body does not identify the target PR
- after success it reports the merged PRs and final SHA; it does not repair local branches or
  turn survivor rewrites into a separate user workflow
- the PR is unmerged precedent, not authority; its atomicity, prefix-selection, and partial-merge
  claims were independently confirmed in the disposable repository

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
- `merge` and apply-mode remote `unstack` when they have selected saved PR identities
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
interrupted native merge even when the merged prefix has left local ancestry. An uncertain
detection or membership read fails the command; it never treats unknown membership as legacy.

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

Queue and auto-merge state do not become jj-stack-wide mutation restrictions. In a repository
without native resources, preserve jj's rewrite flexibility and let the requested GitHub mutation
succeed or return its ordinary rejection. For native create, append, and merge, the native API is
the admission and repository-policy authority. Surface its rejection without adding a preflight
gate or making the PR read-only in unrelated commands.

Draft PRs are valid reviews and native members, but neither merge path selects a draft target.
Apply that lifecycle boundary in every repository:

- `merge` stops its candidate prefix before the first draft
- there is no local readiness override; GitHub decides whether open, non-draft candidates satisfy
  reviews, checks, conflicts, and repository rules

The repository-independent rules are limited:

- GitHub's one-resource-per-PR rule changes jj-stack's review model in every repository
- GitHub's draft lifecycle keeps drafts outside the merge candidate prefix
- restrictions on mutating a native resource govern only the corresponding native operation
- limits of the native resource representation do not invalidate local review topology

In particular, jj-stack must never PATCH the base of a native member or merge one through the
ordinary PR merge API. It must use the native replacement and merge sequences. Native creation
requiring at least two PRs means a one-PR review has no native resource; it does not make that
review invalid. The append-only endpoint means replacement must dissolve and recreate a complete
resource; it does not make a local reorder invalid.

The single merge method per request already matches jj-stack's one-method-per-command behavior.

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

Do not add a queue or auto-merge preflight. The create or append mutation authorizes admission
atomically. PRs already in the exact target resource are not being admitted again. Independently,
jj-stack's active-review discovery still requires a selected saved PR to be open; changing that
lifecycle is outside native admission.

After `replace` dissolves a resource, every desired PR is admitted again during recreation. An
incomplete dissolution is handled by the fresh post-unstack membership check below.

### Historical merged prefix

Native merge retains merged members in the original resource. They form a historical bottom
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

This is one membership model used by submission and merge recovery. Do not normalize successful
merge by dissolving and recreating survivor resources: GitHub deliberately retains the
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

## Merge

`jj stack merge` is the only command that asks GitHub to merge reviewed changes. Cached capability
plus current native membership chooses the GitHub API; it is not a user-selectable transport:

- an active native resource uses one atomic async stack-merge request
- a repository without native support uses the ordinary PR merge API bottom-up
- a one-PR review in a native-capable repository has no native resource and uses one ordinary PR
  merge

There is no direct trunk push, `--via`, `--bypass-readiness`, or compatibility `land` alias.
`merge` changes GitHub state and reports the result. It does not rewrite local history, refresh
survivor review branches, or run cleanup. Selected `sync` is the separate observational command
for reconciling local state afterward.

### Common selection

Resolve one complete selected local review path and all of its saved PR identities. With no
selector, use the stack headed by `@-`. A PR selector may choose a target within that path, but it
does not hide active native members above the target: those members are still resolved because
GitHub may rewrite them.

After any historical merged prefix, candidates are the contiguous open, non-draft PRs from the
bottom. The first draft or closed-unmerged PR blocks it and everything above it. With no explicit
target, choose the highest candidate. An explicit target chooses the bottom-anchored prefix
through that PR. This follows `github/gh-stack#307`.

Do not pre-classify candidates by approval, changes requested, checks, conflicts, mergeability,
branch rules, or merge-queue policy. GitHub evaluates those when it processes the merge. A
rejection is the result of the requested operation, not a reason to add another readiness model
or preflight API.

Before requesting a merge, require every affected PR to match its `ReviewIdentity` and require its
live head to match its `SubmittedBaseline`. Re-read those facts immediately before mutation.
For a native request, this includes survivors above a partial target because GitHub rewrites
them. For the legacy loop, repeat the dependent read before each ordinary merge.

Resolve one merge method for the command. Use
`--merge-method <merge|squash|rebase>` when supplied; otherwise use the sole repository-enabled
method or GitHub's repository default. `--dry-run` performs the same selection and validation and
prints the API shape without mutating GitHub.

### Native async contract

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

The target selects every unmerged member from the bottom through that PR. `sha` is the only
caller-controlled freshness guard the endpoint offers. It protects the target head, not every
lower member. The response's `expected_head_sha` is the accepted value; a request field by that
name is ignored.

The absence of per-member compare-and-swap is part of GitHub's native transaction contract.
Observe every candidate head immediately before the request, but do not decompose a prefix into
one request per PR to manufacture a stronger contract. GitHub owns the atomic group mutation
after that observation.

An accepted request returns `202`, `status: pending`, and details containing its UUID, resolved
merge method, and accepted `expected_head_sha`.

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
`200` response and fresh repository observation.

Every active resource member must resolve in order to a review on the same selected maximal local
path. Historical merged members may precede that active suffix. The target may be any candidate,
including one below active survivors; resolving the complete resource is what makes those
collateral survivor rewrites selected and visible rather than grounds for rejecting a partial
prefix.

The conflict and merge-queue experiments both failed atomically. Treat only terminal `merged` as
success. On terminal failure, report that nothing merged. On terminal success, report the merged
PRs and final trunk SHA and exit `0`, even when GitHub rewrote survivors. Do not turn successful
native behavior into an exit-`1` repair protocol.

GitHub retains merged members as a historical resource prefix. For a partial merge it may retarget
and rewrite every active survivor. That resource transition is authoritative GitHub state.
Selected `sync` accepts the historical prefix and the ordered active-suffix heads as the result of
the native operation; it does not demand tree equivalence, explicit `relink`, or a per-survivor
repair command.

Live testing confirmed that GitHub preserves jj's `change-id` header for both native and ordinary
rebase merges, but not for squash merges. `sync` uses the fetched result: a matching change ID is
the landed successor; otherwise it retires the old local change without relabeling the landed
commit or storing an alias.

### Merge without a native resource

Repositories without native stack support use GitHub's ordinary PR merge API. So does a one-PR
review in a native-capable repository, because GitHub creates no native resource for it. A
multi-PR review in a native-capable repository that unexpectedly has no resource is inconsistent
submission state and points at `submit`; it does not silently select the legacy path.

The ordinary path is the existing GitHub-mediated merge behavior, not a fallback to pushing trunk:

1. process the selected candidate prefix bottom-up
2. immediately before each PR, re-read and verify its identity, exact submitted head, state, and
   current base
3. retarget the PR to trunk when its base is the lower review branch
4. call the ordinary merge API with the expected head SHA and selected merge method
5. stop at the first rejection; PRs GitHub already merged below it remain merged

This path is sequential and therefore is not atomic across the prefix. Do not add compensation,
rollback, durable progress, or a direct-push escape hatch. Report the PRs already merged and point
at selected `sync` before the user retries.

Retain the existing refusal to rebase-merge more than one ordinary PR in one command, because the
first GitHub rewrite invalidates the later reviewed commit identities. Native stack merge does
not have that restriction because GitHub owns the complete group transaction.

An external actor may still push commits directly to trunk. That is recovery evidence for `sync`,
not a supported `merge` transport and not a reason to keep direct-push command code.

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
- when selected `sync` observes a historical merged prefix, the same resource's ordered active
  suffix authorizes the survivor head and base changes produced by the native merge; this is
  resource transition authority, not tree-equivalence evidence

Change one of these only when an implemented native behavior demonstrates a concrete correctness
or recovery requirement. Record that decision here first.

## Test strategy

Read and apply `testing-philosophy.md` before changing tests.

### Fake GitHub

The existing fake covers completed membership and historical-member work. For the remaining
slices, add only the async merge submit and poll endpoints. Model candidate-prefix selection,
target SHA, survivor rewrite, atomic failure, diagnostic `409`, and terminal retry recovery. Do
not implement a general GitHub stack emulator.

### Focused coverage

Only the unfinished slices need new or changed coverage:

- native async merge targets the highest contiguous open, non-draft candidate by default and can
  target an explicit lower candidate
- one native request merges the complete selected prefix; it is never decomposed into per-PR
  requests
- native async merge uses the target-head guard, does not adopt a `409` UUID, and recovers a
  completed retry through terminal observation
- terminal failure changes no repository, PR, branch, or membership state
- rebase results preserve the submitted change ID, while squash results replace it; selected
  `sync` converges either without relabeling the landed commit or storing an alias
- terminal success reports the merged prefix and exits `0` without rewriting local history or
  requiring survivor relink
- selected sync recognizes a historical prefix and accepts the resource's ordered survivor heads
  as GitHub-owned transition state
- legacy `merge` uses the ordinary PR merge API bottom-up and stops after the first rejection
- a one-PR review in a native-capable repository uses the ordinary PR merge API
- a missing resource for a multi-PR native review fails closed instead of selecting the legacy API
- an external direct push cannot make sync retarget or close an open native member
- a draft or closed PR blocks the candidate prefix, while approvals, checks, conflicts, and
  repository rules are left to GitHub

Use one integration test per meaningful cross-system risk and one interruption/retry case. Search
for and replace overlapping landing/recovery coverage rather than increasing its bounded case
count.

## Implementation commits

Each commit is one bounded change with its tests and any temporary-plan update needed to describe
the resulting behavior. A guarded unsupported operation is acceptable between commits; an
operation that mutates partially and then discovers native incompatibility is not.

### Commit 15: native merge synchronization

- resolve native membership for merged-prefix or survivor-drift selected recovery and for global
  candidates before mutation
- make selected and global sync require terminal merged state for a native member
- retain exact-on-trunk recovery for legacy reviews
- treat an observed historical prefix plus its ordered active suffix as GitHub authority for the
  native resource transition
- retire the merged prefix and rebase selected local survivors onto fetched trunk
- let ordinary selected-review synchronization refresh survivors without explicit relink,
  tree-equivalence evidence, or per-survivor repair state

Exit condition: selected sync converges an observed native merge without a special repair
protocol, while global sync still requires terminal merged state before changing a native member.

### Commit 16: native async merge

- add the guarded async submit and poll client operations
- select the contiguous open, non-draft bottom prefix without local approval or mergeability
  policy
- route active native resources to one async request for the selected prefix
- fail closed on a missing resource for a multi-PR review in a native-capable repository
- recover a lost submit response through a later terminal retry, never by adopting a `409` UUID
- report terminal native success without local repair and direct the user to selected `sync`
- add only the merge tests justified by the two concrete GitHub contracts

Exit condition: `merge` always asks GitHub to merge, native resources use one atomic prefix
request, reviews without a native resource retain the ordinary GitHub PR merge path, and no
trunk-push transport remains.

### Commit 17: permanent documentation and plan deletion

- reconcile the finished behavior into `design.md`
- retain the explicit exclusion of support for native stacks of 100 or more reviews
- update `implementation-strategy.md` for the actual final component and test boundaries
- update user docs, help, exit-code documentation, and the bundled skill for the `merge` command
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
- `merge` is always GitHub-mediated and has no direct-push or transport-selection code
- native merge is one atomic bottom-prefix request; legacy merge uses ordinary PR merges
- queue and auto-merge state creates no repository-wide jj restriction; GitHub decides the
  requested mutation, and drafts are never merge candidates
- no GitHub stack topology or operation phase is persisted
- the canonical docs and user guidance describe the finished behavior
- this file has been deleted
