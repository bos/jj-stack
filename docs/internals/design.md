# jj-stack design

This document defines the product rules for `jj-stack`. See
[testing-philosophy.md](testing-philosophy.md) for testing guidance. Command syntax and usage
belong in the [user guide](../README.md) and built-in `--help`.

## Summary

`jj-stack` turns a linear chain of `jj` changes into GitHub pull requests. The `jj` DAG determines
stack topology; local tracking connects each change to its PR and last submitted commit. Change
IDs and PR branch names stay stable across rewrites.

Each invocation supports one Git remote and one GitHub repo, with one PR per change. Cross-repo
stacks and nonlinear local stacks are unsupported. Inspection can still report some unsupported
histories to help users repair them.

## From local changes to pull requests

Suppose the selected local history is:

```text
trunk() <- A <- B <- C
```

`A`, `B`, and `C` are changes, and `C` is the selected head. On GitHub they become:

```text
PR for C: head jj-stack/C, base jj-stack/B
PR for B: head jj-stack/B, base jj-stack/A
PR for A: head jj-stack/A, base trunk
```

## Core concepts

### Change

A logical change is identified by its full `change_id`, which survives rewrites. A commit ID
identifies one immutable snapshot of that change.

Publishing requires a visible, mutable change with a nonblank description, a nonempty diff, and
no divergence or unresolved conflicts. Recovery and inspection have different requirements,
described in their command policies below.

### Local stack

A local stack is a linear chain of changes from a selected head back to the nearest change on
`trunk()`'s first-parent chain. That trunk change is the stack's base and is not itself part of
the stack. A submitted side parent of a merge change on trunk therefore remains in the selected
path until `sync` reconciles it.

Mutating commands that select a local path require a single-parent chain. Other children
elsewhere in the DAG do not invalidate that chain. A local rewrite can propagate to those
children under ordinary `jj` rules; their PRs wait for a command selecting their own path.

### Tracking

A change is **tracked** when one `TrackedPR` record is saved under its full `change_id`. The
record contains:

- `PRIdentity`: the PR number and head branch name
- `SubmittedBaseline`: the exact commit last successfully submitted or explicitly adopted

These two values are created, replaced, and removed together. Partial records are invalid. The
GitHub repo is command context, not a per-change field; tracking must not be carried to another
repo. A branch name or a matching PR without a saved record does not establish tracking.

Two checks recur below:

- **Identity match**: the live PR number and head branch equal the saved `PRIdentity`.
- **Snapshot match**: an identity match whose PR head SHA also equals the submitted baseline.

An identity match establishes which PR to act on. A snapshot match also establishes which version
GitHub reports. [Tracking and recovery](#tracking-and-recovery) defines which commands update
these records.

### PR branches and PR bases

GitHub pull requests are branch-based: every PR needs one head branch and one base branch. The
`jj` DAG supplies neither, so `jj-stack` maintains remote branches to hold the submitted commits.

The initial name is:

```text
<branch-prefix>/<slug-from-subject>-<change_id.short(8)>
```

For example, with the default prefix:

```text
jj-stack/add-cache-index-ypvmkkuo
```

The slug is lowercase ASCII derived from the first description line. The change-ID suffix ties
the branch to the logical change. If two selected changes resolve to the same
name, `submit` stops.

The branch name stays stable after creation, even if the change's description changes.

The GitHub base branch for a change is:

- the parent change's remote branch when the parent is in the local stack
- the explicit submitted base's remote branch for the bottom change selected by `submit --base`
- otherwise the trunk branch

`trunk()` defines the lower bound of a stack without specifying a GitHub branch name. GitHub's
reported default branch is used unless a different branch at local `trunk()` contradicts that
choice. If GitHub reports no default, exactly one branch on the selected remote must point at
`trunk()`. A `trunk()` that falls back to `root()` cannot be resolved this way.

### The reserved branch namespace

A repo reserves exactly one branch namespace for `jj-stack`'s managed branches, named by
`branch_prefix` (`jj-stack` by default). The configured value is used as-is. Ordinary `jj`
bookmarks outside that namespace behave normally. Renaming `branch_prefix` changes only the names
of new PR branches and the fetch exclusion; a branch saved in tracking stays owned under its old
prefix.

The namespace normally stays out of the local `jj` view. `jj`'s default `immutable_heads()` counts
untracked remote bookmarks as immutable, so `doctor --fix` excludes the namespace from ordinary
fetches. Commands warn if that exclusion is missing or overridden, but use the configured fetch
selection and do not stop solely because a PR bookmark is visible.

A visible bookmark in the reserved namespace does not make its commit immutable for `jj-stack`
subprocesses, so a stack can be adopted from a clone that fetched the namespace. The exception
applies when the bookmark matches one saved PR and its submitted commit, or when the commit is
not divergent; a divergent target outside saved tracking stays immutable, so a fetched GitHub
rewrite is not mistaken for a local copy. Two untracked remote bookmarks pointing at the same
commit prevent this exception, even if both are in the reserved namespace. Trunk, tags, and
bookmarks outside the namespace still make their targets immutable. If the submitted commit and
one local rewrite are both visible, the submitted commit is treated as the submitted snapshot
rather than a second local candidate.

An unknown or mismatched bookmark creates no ownership. It remains untouched and does not block an
independent stack. `submit` refuses to claim a colliding visible name for a new PR, while remote
target checks and Git leases still prevent updating or deleting a tracked branch that changed
after inspection.

### GitHub stack objects

A **GitHub stack** is GitHub's server-side object for an ordered group of pull requests. This is
distinct from the local stack derived from the `jj` DAG.

A GitHub stack requires at least two pull requests. Submitting a one-change local stack therefore
creates an ordinary PR. When a later `submit` extends that stack to two or more PRs,
`jj-stack` registers the ordered PRs in a GitHub stack. An existing GitHub stack may later have
only one active member because GitHub retains merged members as history.

GitHub lists merged PRs first in the stack. This document calls the remaining
members **active members**, regardless of whether an individual PR is open, draft, or closed. A
GitHub stack that lists a merged member above an active one stops only the commands that select
it; `unstack --stack <number>` still removes it.

### Workspaces

Several `jj` workspaces can share one repo, and each has its own working-copy commit. Every
workspace's working-copy commit is an ordinary commit for stack discovery when it is described
and nonempty.

Configuration and presentation reads do not snapshot the working copy. Repo operations keep
`jj`'s normal snapshot and checkout behavior.

If `jj` reports that a workspace is stale, the command stops and tells the user to run
`jj workspace update-stale`.

## Command responsibilities

The command policies below define eligibility and mutation rules. This table identifies which
command owns each operation. A **direct merge** is one GitHub completes immediately rather than
through a merge queue.

| Command | Responsibility |
|---|---|
| `view` | Inspect selected local stacks and their current GitHub state. |
| `list` | List local paths with tracked changes, plus orphaned tracked PRs. |
| `submit` | Create PRs and refresh the selected stack; only this command publishes new changes. |
| `sync` | Update a local stack after a merge or native GitHub stack rebase. |
| `sync --all` | Sync stacks after merges and finish eligible PRs without local copies. |
| `merge` | Request a GitHub merge; run sync after a direct merge completes. |
| `unstack` | Remove a GitHub stack; `--local` instead forgets local tracking. |
| `cleanup` | Remove eligible artifacts and links; optionally close explicitly selected PRs. |
| `checkout` | Adopt existing PRs and edit the selected change in the current workspace. |
| `relink` | Repair one change's link to a known PR. |
| `doctor` | Diagnose setup and local leftovers; `--fix` applies the named local repairs. |
| `in-use` | Report whether a valid tracking file exists, without creating one. |
| `completion` | Print shell completion scripts, optionally including a `jj` alias. |

`jj` owns general history editing. There is no standalone `jj-stack rebase` command.

`sync` and `merge` fetch before planning, including during `--dry-run`. A direct merge fetches
again after GitHub completes it. `checkout --pull-request` fetches when the selected PR's head
commit is not already local. Other commands do not fetch. Commands evaluate `trunk()` after any
fetch they perform; without a fetch, they use the locally available trunk.

A dry run previews planned changes without applying them. It does not promise an untouched local
repo: the fetches above and ordinary `jj` working-copy snapshots can still occur.

## Safety rules, in priority order

Within the supported scope, these rules are ordered; a lower rule never weakens a higher one.

1. **Never lose work.** `jj` can undo almost any local mistake; GitHub cannot undo every remote
   mutation. Protect local changes first and treat destructive GitHub operations explicitly.
2. **Check the target.** Before changing a branch, PR, GitHub stack, or repo, confirm it is
   the intended one. Bind the mutation to that identity and version when the platform supports a
   conditional write or lease.
3. **Never guess.** Ambiguous linkage stops the command. Never guess which PR belongs to a change
   or silently adopt one that appeared in place of another.
4. **Merge what was submitted.** Merge only the exact submitted commit, using GitHub's
   expected-head check to bind the request to that commit.
5. **Respect command scope.** Stack-scoped commands mutate only selected PRs, though they may
   inspect the surrounding GitHub stack. Repo-wide cleanup and the other exceptions are listed
   under [Selection](#selection).
6. **Forget deliberately.** Remove tracking only through `unstack --local` or eligible cleanup.

Most stops and warnings should also name a runnable next step when the right action is clear and
the condition is reasonably likely to occur. This UX requirement never weakens a safety rule.

## Tracking and recovery

PR creation, `relink`, and `checkout` create or replace a saved PR identity. `unstack --local`
and cleanup remove it; `sync` uses cleanup to remove eligible records.

The submitted baseline changes only after:

- `submit` or `sync` successfully updates a PR
- `sync` adopts commits rewritten by GitHub, or replaces a GitHub-rebased stack with equivalent
  commits that retain the local change IDs
- `relink` records the observed PR branch target
- `checkout` imports an existing PR

A GitHub merge request does not update the baseline; the automatic sync afterward may do so.

Commands use the current `jj` DAG, remote refs, GitHub state, and saved tracking. GitHub reports
PR state and stack membership; ancestry checks establish whether submitted work reached trunk.
Tracking stores no topology, desired bases, current PR state, operation progress, or aliases
between GitHub commits and local change IDs.

Commands do not automatically replace a tracked missing, closed, moved, or ambiguous PR. A merged
PR directs the user to `sync`; other broken links require explicit repair or cleanup. Once cleanup
removes a closed PR's tracking, `submit` can create a new PR for the change. An open untracked PR
still requires `relink`.

The first tracking write creates the repo's state file. That file remains after its last record is
removed, so `in-use` continues to report adoption. `view`, `list`, and `in-use` never create it.
An unreadable or invalid file blocks commands that load it and names the path to move aside before
using `checkout` or `relink` to restore links. A newer unsupported schema requires an upgrade.

Mutating commands serialize per repo. An interruption can leave completed external effects even
if the command reports failure. A retry computes what remains from current observations; it never
replays a saved plan or selector. The submitted baseline records an acknowledged commit, not
pending work.

## Policies

### Selection

Commands that inspect pull requests use `origin` when it exists, otherwise the sole remote.
Several remotes without `origin` are ambiguous.

Only GitHub's public API is supported. Remote URL hostnames are not validated; the path is
interpreted as a `github.com` owner and repo.

Stack lifecycle commands default to `@` when the working-copy change has a nonblank description
and contents, and to `@-` otherwise. This default does not discard an explicitly selected empty
or undescribed change; publication checks and inspection warnings apply to that selection.

`view` accepts several selectors. A bare change ID, an unambiguous prefix, or a linked PR selects
the whole local stack containing that change. If several stack heads descend from it, selection
is ambiguous and stops. Other revsets select the stack ending at the specified revision.

When the selector is a change ID or linked PR, selection prefers the unique mutable copy outside
trunk's first-parent path.

`merge <revset>` selects the stack ending at the specified revision. `merge --pull-request`
selects the complete local stack containing the named PR, but merges only through that PR.
`relink` requires both a change and a PR.

These modes use a different scope:

- `sync --all`, which cannot be combined with a selector
- `cleanup` without a selector, which considers every tracked change in the repo
- `cleanup --pull-request <pr>`, which may select one tracked PR whose local change is gone, and
  `cleanup --pull-request orphans`, which selects all such PRs
- `unstack --stack <number>`, which selects one GitHub stack without requiring local tracking

Apart from these modes, PR mutations stay within the selected stack. Ambiguous selectors stop
with an error.

### Identity and mutation preconditions

Before the first mutation, a command validates the identity on which every planned selected
mutation depends. This prevents a pre-existing mismatch from being discovered only after an
earlier selected PR has changed.

The command-specific planning requirements are:

- `submit` requires an identity match for every tracked selected change and observes the exact
  remote target of each PR branch. Normally that target is the submitted baseline. It may
  already be the change's current commit after an interrupted submit, but only when the identity
  matches and the PR head agrees with that same commit. Any other target stops the command.
- `merge` requires the current local commit and remote PR branch ref both to equal
  `SubmittedBaseline.commit_id`, plus a live snapshot match. Tree or diff equivalence is not
  sufficient.
- `sync --all` requires a snapshot match before retargeting, closing, or cleaning up a PR.
- cleanup requires an identity match before closing a PR, deleting artifacts, or removing saved
  links.

A renamed head, missing PR, unexpected branch target, or competing PR found during planning
stops the command and names `relink` or `unstack --local`, depending on whether the user needs to
repair or forget the saved link.

### Submit and branch transport

`submit` publishes only the selected stack, bottom-up. It creates missing PRs, moves existing
PR branches, updates PR bases and content, and refreshes GitHub stack membership.

Before any remote mutation, `submit` requires a reachable repo and an available GitHub Stacks
API, then observes PRs and complete stack membership.

`submit --base B H` publishes the changes above `B` through `H`, written `(B, H]`. `B` must be
an ancestor of `H` on the selected single-parent path and is excluded from every mutation. Its
local commit, submitted baseline, PR branch, and live PR head must be the same commit, and its
saved identity must match an open PR.

An externally moved or missing base PR branch is never overwritten by `submit`. `jj-stack`
cannot repair it automatically: the user must restore that branch to its saved submitted commit
before retrying.

No boundary is stored or inferred. Repeat `--base` on each refresh; omitting it selects the stack
back to trunk and may include or regroup the parent path. Once the base PR merges, `submit --base`
stops, even if a higher PR in the parent stack remains open. The user syncs the parent, then
rebases the child and its descendants onto `trunk()` and submits the child without `--base`
before merging it. Other child stacks wait for the user to select them.

`submit` rebuilds GitHub stacks under the [membership rules](#github-stack-membership). When
only one active PR remains in its selection, it becomes an ordinary PR.

All selected PR branches move in one atomic push. Every update carries the target
`jj-stack` observed for that GitHub branch, including expected absence for a new branch. The
push binds each update to that target with a Git lease. If any ref moved, the whole push
fails; there is no sequential fallback. An untracked branch is accepted only for first-submit
recovery after an interrupted push: exactly one managed branch may end in the selected short
change ID, and its commit must carry the full change-ID header.

If an intermediate parent PR is not open, `submit` stops; it does not skip that parent when
choosing the child's base.

A topology rewrite counts as a PR update even when the tree diff is unchanged. During a
rewrite, `submit` may temporarily retarget selected PRs to prevent GitHub from auto-closing a PR
whose new base contains its head. An interruption may leave bases at their old value, trunk, or
the desired parent; a rerun finishes the update without replacing PRs.

An open PR currently in a merge queue is not updated. `submit` stops the selected stack before
moving any PR branch or changing any PR, and tells the user to wait for GitHub to merge it.

### Merge

`merge` considers consecutive PRs from the bottom of the selected stack. Each PR must be open
and non-draft. Its local change must still exist and have no divergence or unresolved conflicts.
The first draft or closed, unmerged PR ends the sequence.

GitHub receives one asynchronous merge request for the selected PRs, whether it contains one
PR or several. A multi-PR request acts on the matching GitHub stack. Every request passes the
expected head commit of the top selected PR.

Before the request, `merge` asks whether the trunk branch has a merge queue, using GitHub's merge
queue object or a `MERGE_QUEUE` branch rule. If that lookup fails, `merge` stops with the GitHub
error before requesting anything. It sends the explicit action `merge_queue` when a queue is
found and `direct_merge` otherwise.

A `merged` result means a direct merge completed; `merge` then fetches and syncs the whole
selected stack before returning, including changes above the last merged PR and commits GitHub
rewrote. An `enqueued` result means GitHub accepted the selected PRs into the queue. The command
succeeds, but the user must wait for the queued merge to finish before running `sync`. A rejection
leaves local changes unchanged.

Automatic reconciliation identifies the containing stack by the full change ID of the head
resolved before the merge request. It does not reinterpret the original revset
after fetching the changed trunk.

GitHub merge success and local reconciliation are separate outcomes. If GitHub completes the
merge but the automatic sync stops, `merge` returns the sync failure status and says that the
GitHub merge must not be retried.

For a direct merge, the merge method comes from `--method`, otherwise from `merge_method` in
repo configuration. Without either choice, `merge` uses the repo's only allowed method when
there is just one. If several methods are allowed, it prefers `rebase`, then `squash`, then
`merge`, unless any change in the complete selected local stack has a commit signature. Because
merging can discard commit signatures, a signed stack requires an explicit flag or setting when
several methods are allowed. This includes changes not being merged yet, which GitHub may rewrite
when earlier changes are merged. Signature presence is observed from the selected commits
without verifying trust or storing it in tracking. This requirement makes the merge method an
explicit user choice; it does not promise signature preservation.

A configured method the repo does not allow is refused by name before any request goes out.
A merge queue chooses its own method, so the request omits it and signed stacks need no explicit
method; an explicit `--method` produces a warning and is ignored.

Before merging or enqueueing an ordinary PR, `jj-stack` retargets it to the trunk branch and
sends its expected head commit. Trunk advancing does not block the merge because the request
depends on the branch name, not its commit. One PR selected from a larger GitHub stack remains
a stack merge and is not retargeted this way.

A submitted change GitHub already merged is still a stop, decided from the pull request's own
reported state rather than from trunk position. The diagnostic names `sync`, which checks trunk
before removing the local copy.

### Repo policy

A merge initiated through GitHub's UI, auto-merge, or another client is supported. Rebasing a
complete native GitHub stack through GitHub's UI is also supported. A later `sync` reconciles
either result under the rules below.

`jj-stack` does not duplicate repo policy. Apart from choosing direct merge or queue
routing for the trunk branch, it does not preflight approvals, checks, conflicts, or auto-merge
state across the repo. GitHub applies those rules to the requested GitHub stack or
single-PR mutation, and `jj-stack` reports the result.

A rejected merge must explain what the user can do next: rebase onto trunk, resolve, and submit
again for a conflict; address the failing check or repo rule on GitHub otherwise.

### Trunk evidence and sync

There are two ways to check that submitted work reached trunk. Both compare the PR with its
saved record and check commit ancestry; a PR's merged state alone does not establish that its
work reached this repo's trunk:

- **Submitted commit on trunk**: the baseline is an ancestor of trunk and the live
  PR is a snapshot match. A PR belonging to a GitHub stack must also report merged before `sync`
  may act on it.
- **Selected PR's rewritten merge result on trunk**: the saved PR is an identity match, reports
  merged, still reports the submitted head, and reports a merge-result commit that is an ancestor
  of trunk. This covers squash and rebase results.

`sync` may use either result. `sync --all` uses each rewritten merge result to select and
reconcile its affected local paths; it does not apply one PR's evidence to unrelated work. It
continues with independent stacks when one is blocked. If no local copy remains, it uses that
evidence only for ordinary cleanup. A native GitHub stack rebase without a merge requires `sync`
for that stack; `sync --all` discovers work from merge evidence.

When an unmerged local change sits below a submitted change whose submitted commit or merge result
is on trunk, `sync` stops without mutation. Rebasing would silently decide whether that local
change belongs before or after the merged work. The diagnostic names the changes and the
submitted, local, and trunk commits, then gives a `jj log` command for inspecting both histories.
The user orders the changes with `jj`, then syncs a remaining mutable submitted head or runs
cleanup if no submitted local copy remains.

Here unpublished local work means a mutable, non-empty change whose commit is not its submitted
baseline. An empty change modifies no files relative to its parent, so removing it discards no
content.

#### Updating local changes after a merge

`sync` updates the remaining local changes only when:

- rewriting them would not discard unpublished local work
- no surviving change has multiple mutable local copies (a fetched GitHub rewrite is immutable)
- no unsubmitted change sits between remaining submitted changes
- every surviving pull request outside a GitHub stack's active members is open and still at the
  submitted or local commit; a moved or missing PR branch stops `sync` before any rewrite and
  names the repair. Changes GitHub rewrote as part of a stack merge or rebase follow the rules
  below.

If any selected open PR is still in a merge queue, `sync` leaves the selected stack unchanged.
Once GitHub no longer reports it queued, ordinary trunk evidence determines whether `sync`
reconciles merged work or has nothing to do.

It rebases surviving changes onto trunk even when they contain conflicts. If a submitted
change remains conflicted, the local rebase stays in place but its PR is not updated. The
user resolves the conflict with `jj` and runs `submit` for the remaining stack.

If a workspace directly has an obsolete merged change checked out, `sync` does not remove that
change. Its diagnostic identifies the workspace and gives commands to move it to trunk or forget
it and move its directory to the trash. A workspace on a surviving child does not block its
ordinary rebase.

If another local path still depends on a merged change after the rebase, `sync` leaves the
change and its tracking in place and names each other stack that still needs `sync`.

After updating the remaining PRs, `sync` invokes [cleanup](#unstack-and-cleanup) for merged PRs
that no local path still needs. Its output describes local updates and cleanup, including when
no PRs remain.

`sync` never rebases merely because trunk advanced. Ordinary `jj rebase` owns that workflow.

GitHub preserves `jj`'s `change-id` commit header through rebase merges of PRs, but not squash
merges. A matching full change ID on trunk identifies the successor rather than
an arbitrary visible side copy. When trunk has no matching change ID, `sync` removes the
old local change without relabeling that commit.

When GitHub merges part of a stack and rewrites the remaining PRs, GitHub's rewrite of
each remaining change starts from its submitted baseline. If every remaining local change is still
at its baseline, `sync` uses the commits GitHub reports rather than replaying equivalent
diffs; if any remaining change has local edits, `sync` adopts none, rebases the remaining changes
onto trunk, records GitHub's reported heads as their baselines, and republishes them. It
accepts those heads and bases only while a merged PR in the same GitHub stack matches its saved
record and its merge result is on trunk.

#### Native GitHub stack rebase

GitHub's native stack rebase rewrites every active member and removes `jj`'s change-ID
commit headers. With no merged member, those remote commits cannot become the identity of the
local changes. `sync` recognizes this result only when all of these observations agree:

- every member of the GitHub stack is among the selected tracked PRs, in their local parent
  order
- every PR still uses its saved head branch and the expected base branch
- every PR head and PR branch moved from its submitted baseline to the same reported commit
- the reported commits form one first-parent chain rooted at trunk
- none of the selected local changes is divergent

`sync` then computes a rebase of the original local changes without first changing the local DAG.
The computed change IDs must remain the selected change IDs, conflicts are rejected, and each
computed commit tree must exactly equal the corresponding GitHub commit tree. This comparison is
also the recovery check when a previous run integrated the local rebase but failed before moving
the PR branches.

After these checks pass, `sync` integrates the local rebase, atomically replaces every rewritten
PR branch with leases requiring each branch to remain at its observed GitHub head, and records the
resulting local commits as the submitted baselines. A changed lease leaves the local rebase in
place and advances no baseline; a retry compares its trees with the current GitHub stack.

### GitHub stack membership

`merge`, `sync`, and locally selected `unstack` stop before mutation if the selection spans two
active GitHub stacks or leaves out an active member of a stack it touches. The diagnostic names
`jj-stack unstack --stack <number>` when removing the stack would allow the command to proceed.

`submit` reconciles GitHub stacks from the selected local path. It may dissolve any number of
GitHub stacks whose active members are all selected. It may also dissolve one partially selected
GitHub stack when the selection is a maximal local path and touches no other GitHub stack. A
non-maximal selection could silently truncate a still-valid stack, so it stops before mutation.
An unselected active member does not trigger that guard when its observed PR number, branch, and
head still match the saved PR number, branch, and submitted baseline, and its tracked change has
no visible off-trunk copy. That change cannot be on an extension of the selected path. Rebuilding
the stack leaves the orphaned pull request open and retains its tracking.
Likewise, a selection that partly overlaps one GitHub stack while including any previously
submitted PR outside that resource stops; the user submits the source path first, then the
destination path.

Merged members do not have to be selected. If selected PRs appear only as history, one matching
GitHub stack may be observed without mutation; more than one is ambiguous and stops the command.

Changing the base of an active GitHub stack member requires dissolving that GitHub stack first
because GitHub offers no single-member removal. `jj-stack` asks GitHub to dissolve the GitHub
stack it inspected. If GitHub retains a queued or otherwise locked active member, the operation
stops before changing any branch or base. Historical merged members may remain in the resource;
they do not block the mutation because they are no longer active.

### Derived artifacts

A subsequent submit refreshes a PR description only if it still matches the last automated PR
description. Otherwise it is preserved; explicitly supplied text still takes effect. See
[pull request descriptions](../reference/descriptions.md).

When a submit supplies no stack overview, the existing overview text is preserved and moves to
the current head PR if the stack grows. An explicitly supplied overview replaces it. A lone PR has
no overview comment. New PRs are created in the requested draft state.

Submit maintains a per-PR revision-history comment with the most recent versions available from
GitHub's force-push timeline. It shows readers how the PR evolved, remains after cleanup, and
never determines topology or mutation eligibility.

Without an edited draft choice, existing PRs become draft only with `--draft=all` and become ready
only with `--open`; plain `submit --draft` leaves their draft state unchanged. With `--edit`,
GitHub's current state and those command-wide defaults populate one editable draft choice per
change. The validated document then determines each selected PR's draft state without adding local
state. A newly generated editor file remains until the entire submit succeeds. If the command
stops, the user can pass that file to `--resume-edit`; the retry re-observes the local stack and
GitHub, and accepts the file only when it names exactly the currently selected changes. The file
carries no submit plan or phase.

`--reviewers` and `--team-reviewers` request the named reviewers even when a PR is otherwise
unchanged and never remove omitted reviewers. `--re-request` acts on an otherwise unchanged PR.
For each user, it considers the latest approval, request for changes, or dismissal, and requests
another review only if that state is approved or changes requested. Comment-only reviews do not
qualify. Re-requesting adds requests and never cancels a pending one.

An explicit `--label` request applies even when a PR is otherwise unchanged; configured labels
alone do not turn a no-op submit into a metadata update. Labels are also additive; omitted labels
are never removed.

Default PR bodies derived from change descriptions unfold Markdown soft line breaks into spaces.
Markdown block boundaries, code, tables, and explicit hard line breaks remain unchanged.

### Unstack and cleanup

`unstack` removes the selected GitHub stack and leaves every pull request, PR branch,
overview comment, and tracking record unchanged. Rerunning it after the stack is gone is safe.

`unstack --local` removes tracking for the selected local stack without checking PR lifecycle or
trunk evidence. It leaves GitHub and local history unchanged.

Closing pull requests through GitHub's UI or `gh pr close` is supported and leaves tracking in
place. To create new PRs for the same changes, close the old PRs, run cleanup, then submit again.

`cleanup --pull-request <pr> --close` and `cleanup --pull-request orphans --close` combine closure
and cleanup for an explicit saved selection. The flag is invalid without `--pull-request`.
Identity, PR-branch ownership, open dependents, GitHub stack membership, and the managed overview
comment are all checked before closing an open PR. Explicit cleanup first retargets the PR to
trunk so that GitHub can still reopen it once its base branch is deleted. `sync` closes a PR whose
submitted commit is on trunk without retargeting: GitHub rejects changing a PR's base
to a branch that already contains its head, and reopening already-landed work protects nothing.
A PR already closed or merged skips closure and follows ordinary cleanup. A closure failure
stops later selected mutations.

Cleanup removes the managed overview comment and the saved PR branch at its observed commit
before removing the tracking record. These rules apply whether cleanup runs directly or at the
end of `sync`.

A PR is eligible for cleanup only when:

- GitHub reports the saved PR closed or merged
- for a merged PR, no visible mutable local copy still needs `sync`
- no PR in the same repo that is open, or closed but still reopenable, uses the saved head ref
  as its base
- no active member of a GitHub stack still needs the branch

A closed PR counts while its own head branch still exists. GitHub cannot reopen it without a
base branch or retarget it while it is closed, so deleting its base would require restoring that
branch to reopen it. If its head branch is already absent, cleanup no longer treats that PR as a
dependent needing the base. Merged PRs do not count as dependents.

Branch dependencies come from PR bases on GitHub, not local descendants. A PR that cannot be
inspected is skipped. Once mutation starts, a failure stops cleanup and leaves later records for
a rerun.

### Adoption and repair

`checkout --pull-request` treats the selected PR as the head of the remote chain to adopt and
edit. `--revset` selects an exact local head. `--pick` combines locally tracked paths with active
GitHub stack resources, showing each GitHub stack's number, top active PR, base, size, status, and
whether it is already local. Choosing a GitHub-only or partially tracked stack passes its top
active PR through the same adoption path as `--pull-request`; the picker does not create another
tracking path.

When the selected PR's exact head commit is not visible locally, `checkout` fetches ordinary
remote state and imports it through a temporary ref as a visible mutable commit; if that change
already exists locally at another commit, or the head sits above the PR's own change, `checkout`
reports the extra copies or commits and does not choose between them. It validates the complete
selected stack and saves any new tracking before it runs `jj edit` on the exact head commit it
observed. If the workspace move fails after adoption, a rerun observes the saved tracking and
retries the move. The command does not rebase changes, restack descendants, or mutate PRs, and
it leaves no PR bookmarks behind.

`relink` replaces tracking for one change after checking that the PR is open, its head branch
belongs to the same repo, and neither the PR nor branch is linked to another change. The branch
name must end in the selected change's short-ID suffix. A remote commit whose header and branch
identify another change is refused; `relink` cannot transfer a PR to a replacement change ID.

The remote target must equal the local commit or saved baseline. `--replace-remote` waives only
this check: it records the remote commit as the baseline so a later `submit` can replace it. It
does not waive identity or branch checks.

`doctor` observes setup, push permission on the selected repo, GitHub Stacks API availability, and
local leftovers from interrupted `checkout` or `sync`. It changes nothing without `--fix`; its
repairs are restoring the reserved PR-branch exclusion in remote fetch configuration, forgetting
untracked remote bookmarks in that namespace, and removing the temporary import ref and bookmark.
`checkout` and `sync` also remove that leftover when they start. It never mutates GitHub.

### Inspection

`in-use` is a silent local predicate. It exits 0 when the repo has a valid tracking file and 1
when the file is absent. An invalid file or failure to locate a jj repo is an error,
not a negative result, and exits 11.

`view` and `list` are read-only. For local stack rows, both ask GitHub for current PR state
without fetching. Orphan rows in `list` show saved identity only; they do not claim to report the
PR's live state.

Both commands project paths from the local `jj` DAG. One projected path may therefore show
changes that explicit submit boundaries placed in several native GitHub stacks. Inspection does
not segment the path by GitHub resource or infer an omitted submit boundary.

Both report whether an open PR has a merge-queue entry; position and intermediate queue phases
are not modeled.

Neither command guesses. A change with no saved PR identity is reported as not submitted,
even if a PR happens to use the branch name that change would generate. A saved PR is always
the one reported; a different open PR on its branch is a warning. An open PR whose head is
neither the local commit nor the submitted baseline is reported as moved, not as healthy.

Inspection tolerates fetched side copies of merged changes. `view` walks past immutable or
divergent side copies when a supported path remains, and shows merged local work as
`sync needed` with a `sync` hint. If no path remains, it returns a targeted selection error.

Empty, undescribed, conflicted, and merge changes produce warnings, but do not by themselves make
a report incomplete. A merge warning states that only the first-parent path is shown.

`view` and `list` share the rule for incomplete reports: an unmerged divergent change, ambiguous
PR, failed PR lookup, broken saved link, or unobserved saved PR state makes the report incomplete.
A per-change lookup failure affects only its row; a failure before rows can be built returns its
own error code. When several local changes claim one branch, `list` warns and skips live
inspection for that branch. These incomplete reports exit 10.

`view` and `submit` render stack rows through the user's `jj log` formatting. `--json`
follows [`docs/json-output.schema.json`](../json-output.schema.json) and exposes no cache state,
raw remote targets, or tracking records.

### Rewrite behavior

Abandoning a change leaves its PR orphaned; other stacks leave that PR unchanged. Close it and
remove its branch and saved link with
`cleanup --pull-request <pr> --close`.

#### Cross-stack rewrites

The [membership rules](#github-stack-membership) determine which GitHub stacks a submit
replaces:

- **Move changes**: submit the source stack before the destination so the moved PRs leave their
  old stack first.
- **Split a stack**: keep the shared fork in the parent stack and submit each child separately
  with `--base`. The first child submit dissolves the old stack; the other children wait for
  their own submits.
- **Join stacks**: submit the resulting local chain to replace the old stacks with one stack.

Stacks not yet resubmitted may still show old overview comments. `list` names each stack that a
`submit` would refresh, and `view` lists the changes involved.

## CLI contract

`help --all` adds advanced commands and hidden global options to ordinary top-level help.
The hidden `help --website-reference` option emits the complete command reference as the Markdown
and HTML body of the website's CLI reference page.

Running the executable without a subcommand is equivalent to `view` without arguments.

### Exit codes

Exit codes are a public interface; their table lives in
[automation](../reference/automation.md#exit-codes) and their implementation in
[`errors.py`](../../src/jj_stack/errors.py). Codes 7–9 remain reserved for `gh stack` meanings
that have no `jj-stack` equivalent. Errors must distinguish a planning stop from a failure after
completed work.

## References

The design relies on these upstream `jj` references:

- [glossary](https://docs.jj-vcs.dev/latest/glossary/) for change IDs, rewrites, and visible
  commits
- [bookmarks](https://docs.jj-vcs.dev/latest/bookmarks/) for bookmark behavior, tracking, and
  push safety
- [GitHub workflow](https://docs.jj-vcs.dev/latest/github/) for GitHub integration and `gh`
  caveats
- [configuration](https://docs.jj-vcs.dev/latest/config/) for `jj` configuration
- [templates](https://docs.jj-vcs.dev/latest/templates/) for machine-readable template output
- [FAQ](https://docs.jj-vcs.dev/latest/faq/) for integration guidance
- [technical architecture](https://docs.jj-vcs.dev/latest/technical/architecture/) for why
  `.jj` internals are not an external extension surface
