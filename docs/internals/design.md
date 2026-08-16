# Stacked GitHub review from `jj`: design

This is the canonical product specification for `jj-stack`.
Implementation structure belongs in `implementation-strategy.md`; testing guidance belongs in the
testing and review documents.

## Summary

`jj-stack` turns a linear chain of `jj` changes into a stack of GitHub pull requests without
using side metadata to determine stack topology.

The model is small:

- one review change is one visible mutable `jj` change, identified by its full `change_id`
- one local review stack is a linear chain of those changes from a selected head toward
  `trunk()`
- each tracked change has one stable remote review branch, used as that change's PR head
- the local stack is rediscovered from the `jj` DAG on every run, not from a saved parent map

The only per-change state `jj-stack` saves locally is the PR and branch attached to each change
and the exact commit last sent for review. The existence of that repository's tracking file is
also the durable local signal that the repository has adopted jj-stack. Everything else is
observed or derived.

Three goals shape the design beyond that model: stacked GitHub PRs should feel natural in a `jj`
workflow, the tool should be easy to use, and review branch names should stay stable across
rewrite-heavy review.

## From local changes to pull requests

Suppose the selected local history is:

```text
trunk() <- A <- B <- C
```

`A`, `B`, and `C` are review changes, and `C` is the selected head. On GitHub they become:

```text
PR for C: head jj-stack/C, base jj-stack/B
PR for B: head jj-stack/B, base jj-stack/A
PR for A: head jj-stack/A, base trunk
```

The actual branch names include a subject slug and change-ID suffix, but those names are only
GitHub transport. The `jj` parent relation determines the stack and each PR's base.

The normal lifecycle is:

1. Use `jj` to create, reorder, split, squash, or rebase local changes.
2. Use `jj-stack view` to inspect the stack, and `jj-stack list` to see tracked stacks.
3. Use `jj-stack submit` to create or refresh the PRs for that selected stack.
4. After another local rewrite, run `submit` again; existing reviews follow their change IDs.
5. Use `jj-stack merge` to ask GitHub to merge a reviewed prefix from the bottom. When GitHub
   completes a direct merge, the same command fetches its result and reconciles the remaining
   local changes and reviews with what reached trunk.
6. After a queued merge, a merge completed outside `jj-stack`, or a native GitHub stack rebase,
   run `jj-stack sync` for that selected stack once GitHub has finished.

## Core concepts

### Review change

A review change is one visible mutable `jj` change, identified by full `change_id`. A `change_id`
is the durable identity of a logical change across rewrites; a Git commit ID is not. A review
change's current commit ID, remote branch name, and diff base are not part of its identity.

"Visible mutable" follows `jj`'s own revsets:

- visible: the commit is in `visible()`, not a hidden predecessor
- mutable: the commit is in `mutable()`

A commit meeting those conditions is *reviewable*: eligible to become a review change. Two extra
eligibility rules apply to the working copy. An empty working-copy commit is not reviewable, and
an undescribed working-copy commit cannot be selected for review until the user describes it.

### Local review stack

A local review stack is the chain of single-parent commits from a selected head back to the
nearest commit on `trunk()`'s first-parent chain. That commit is the stack's base and is not
itself part of the stack. A reviewed side parent of a merge commit on trunk therefore remains in
the selected path until `sync` reconciles it.

`submit --base B H` is the one explicit exception to the trunk boundary. `B` must be on that
single-parent chain below `H`; the command selects `(B, H]` and treats `B` as read-only base
context rather than part of the submitted stack. This boundary is command input, not saved
topology. Every later child submit must name it again.

Commands that change review state support only linear stacks, so their walk follows each commit's
sole parent. They reject a merge commit inside the selected chain and a divergent review change.
`view` is best-effort inspection: it reports the first-parent path through a merge and warns about
the omitted shape rather than requiring a rewrite before showing output. Unresolved conflicts do
not break the shape, so `view` and `list` report a conflicted change. `submit` and `merge` refuse
to act on one. `sync` may leave a conflicted rebase in the local DAG, but it does not move that
change's review branch or update its pull request. After resolving the conflicts, the user runs
`submit` to send the new commit for review.

Commands plan review mutations from the selected chain. Other visible children elsewhere in the
DAG are not an error. A `sync` rebase may also move descendants when `jj` propagates a rewrite,
but it never updates reviews outside the selected chain.

After a rebase merge is fetched, the immutable commit on trunk and the superseded local commit can
share one change ID. A change-ID or linked-PR selector chooses the unique mutable local copy
outside trunk. It stops if several mutable copies match or if every match is already on trunk. An
explicit revision expression can still select a particular trunk commit. A reviewed side parent
left by a stack merge remains selectable until `sync` reconciles it.

### Tracking

`jj-stack` remembers two facts about each change it has published:

- the **review identity**: which GitHub PR and which review branch belong to that change
- the **submitted baseline**: the exact commit last successfully sent for review

A change is **tracked** when both facts are saved as one pair, and **untracked** otherwise. The
state model never stores only one half. A predicted branch name, or a PR that happens to use one,
does not make a change tracked.

Tracking records which review a change owns, which prevents mutating the wrong one. It does not
show on its own that a mutation is safe.

### Review branches and PR bases

GitHub review is branch-based: every PR needs one head branch and one base branch. The `jj` DAG
supplies neither, so `jj-stack` maintains remote branches purely as transport.

Each tracked review change has exactly one Git branch used as its GitHub PR head. These branches
normally stay remote-only and outside the local `jj` view.

The initial name is:

```text
<branch-prefix>/<slug-from-subject>-<change_id.short(8)>
```

For example, with the default prefix:

```text
jj-stack/add-cache-index-ypvmkkuo
```

The slug is lowercase ASCII derived from the first description line. The change-ID suffix ties
the branch to the logical change. In the extremely unlikely case that two selected changes
resolve to the same name, `submit` stops.

The subject is only used once, when creating the initial name for a branch. Once a review is
tracked, the branch name stays stable. Commands do not rename or replace it because the
description changed.

The GitHub base branch for a review change is:

- the parent review change's remote branch when the parent is in the local review stack
- the explicit reviewed base's remote branch for the bottom change selected by `submit --base`
- otherwise the trunk branch

`trunk()` defines the lower bound of a stack without specifying a GitHub branch name. GitHub's
reported default branch is used unless a different branch at local `trunk()` proves that choice
inconsistent. If GitHub reports no default, exactly one branch on the selected remote must point
at `trunk()`. A `trunk()` that falls back to `root()` cannot be resolved this way.

### The reserved branch namespace

A repository reserves exactly one branch namespace for `jj-stack`'s managed branches, named by
`branch_prefix` (`jj-stack` by default). The configured value is used as-is. Ordinary `jj`
bookmarks outside that namespace behave normally.

The namespace normally stays out of the local `jj` view. `jj`'s default `immutable_heads()` counts
untracked remote bookmarks as immutable, so `doctor --fix` excludes the namespace from ordinary
fetches. Missing or overridden fetch isolation is advisory; commands use the configured fetch
selection without changing it and do not stop merely because a review bookmark is visible.

A visible bookmark receives an immutability exception only when it matches one saved review and
its submitted commit. For `jj-stack` subprocesses, that exact bookmark does not make the commit
immutable; trunk, tags, and other untracked bookmarks still do. If the submitted commit and one
local rewrite are both visible, the submitted commit is treated as the reviewed snapshot rather
than a second local candidate.

An unknown or mismatched bookmark creates no ownership. It remains untouched and does not block an
independent stack. `submit` refuses to claim a colliding visible name for a new review, while live
remote target checks and exact leases continue to guard moves and deletion of tracked branches.

### GitHub stack objects

A **GitHub stack** is GitHub's server-side object for an ordered group of pull requests. This is
distinct from the local review stack derived from the `jj` DAG.

A GitHub stack requires at least two pull requests. A review first submitted with one PR
therefore uses an ordinary PR. When a later `submit` extends that review to two or more PRs,
`jj-stack` registers the ordered PRs in a GitHub stack. An existing GitHub stack may later have
only one active member because GitHub retains merged members as history.

GitHub reports merged members as a historical bottom prefix. This document calls the remaining
members **active members**, regardless of whether an individual PR is open, draft, or closed.

### Workspaces

Several `jj` workspaces can share one repository, and each has its own working-copy commit.
Repository-wide discovery includes a working-copy commit only when it is tracked, described, and
nonempty. A stack command defaults to `@` under the same conditions and to `@-` otherwise.

Configuration and presentation reads do not snapshot the working copy. Repository operations keep
`jj`'s normal snapshot and checkout behavior.

If `jj` reports that a workspace is stale, the command stops and tells the user to run
`jj workspace update-stale`.

## Commands and lifecycle

This is a map of command purpose and scope. Later policy sections define exact eligibility,
evidence, and mutation rules.

- **`view`** inspects one or more selected stacks and reports local, remote-branch, and GitHub
  state. With no selector it uses the default under [Selection](#selection).
- **`list`** reports local paths containing a tracked change and orphaned tracked reviews. It does
  not inventory wholly untracked stacks.
- **`submit`** publishes the selected stack. It is the only command that creates a PR or
  publishes a never-submitted change.
- **`sync`** reconciles the selected stack after reviewed work lands or GitHub rebases the whole
  active stack. It may rewrite surviving local changes, update their existing reviews, and clean
  up merged reviews after the local update succeeds. It never creates a PR.
- **`sync --all`** discovers every affected local stack and applies ordinary selected-stack
  reconciliation to each one in turn. It also finishes reviews whose exact submitted commits are
  on trunk and whose local changes are gone. A blocked stack does not prevent independent stacks
  from continuing.
- **`merge`** is the only command that asks GitHub to merge. It never pushes trunk. After GitHub
  completes a direct merge, it immediately performs the same selected-stack reconciliation as
  `sync`; queue acceptance leaves local history alone.
- **`unstack`** removes GitHub's stack grouping while leaving its pull requests open. A GitHub
  stack number selects the remote resource directly; otherwise a local review stack selects its
  matching GitHub stack. `--local` only forgets local tracking and does not change GitHub.
- **`cleanup`** removes eligible branches, managed overview comments, and tracking for closed or
  merged reviews. With an explicit pull-request selector, `--close` first closes selected open
  reviews. `sync` invokes cleanup after reconciling merged work. The standalone command handles
  review closure, closed reviews, and cleanup retries; with no selector it checks the repository,
  while a revision or pull request limits it to the named review.
- **`checkout`** adopts review state already on GitHub and edits the selected change in the
  current workspace.
- **`relink`** attaches one known PR and same-repository head branch to one selected change when
  the user knows the identity but the tool cannot prove it.
- **`doctor`** reports setup, connectivity, and observable leftovers from interrupted local
  operations. `--fix` applies only the local repairs it names.
- **`in-use`** silently reports whether a valid tracking file exists for this local repository.
  It does not snapshot the working copy, read GitHub, or create tracking.
- **`completion`** prints shell completion scripts and inspects nothing. With `--jj-alias`, the
  script also completes that `jj` command alias as `jj-stack` while preserving completion for
  other `jj` commands.

There is no standalone `rebase` command; `jj` owns general descendant rewrites.

`sync` and `merge` run `jj git fetch` themselves before they act. `checkout --pull-request`
fetches when the selected PR's exact head commit is not already local. A direct `merge` fetches
once while preparing the GitHub request and again after GitHub completes it so local
reconciliation observes the result. No other command fetches, so when local trunk is stale the
user runs `jj git fetch`. **Fetched trunk** below always means `trunk()` as evaluated after the
running command's relevant fetch.

## Sources of truth

Three sources answer questions about a review, each for a different domain:

1. The **`jj` DAG** determines which local changes exist, how they are related, and what they
   contain.
2. **GitHub** reports PR existence, lifecycle, reviews, GitHub stack membership, and merge
   results. Whether work actually reached trunk is proven separately by ancestry from it.
3. **Local tracking** records only the review identity and submitted baseline of each change. It
   prevents mutation of the wrong review but cannot make a mutation safe on its own.

## Safety rules, in priority order

Within the supported scope, these rules are ordered; a lower rule never weakens a higher one.

1. **Never lose work.** `jj` can undo almost any local mistake; GitHub cannot undo every remote
   mutation. Protect local commits first and treat destructive GitHub operations explicitly.
2. **Check the target.** Before changing a branch, PR, GitHub stack, or repository, confirm it is
   the intended one. Bind the mutation to that identity and version when the platform supports a
   conditional write or lease.
3. **Never guess.** Ambiguous linkage stops the command. Never guess which PR belongs to a change
   or silently adopt one that appeared in place of another.
4. **Merge what was reviewed.** Merge only the exact commit submitted for review, using GitHub's
   expected-head check to bind the request to that commit.
5. **Stay in the selected stack.** Stack-scoped commands mutate only selected reviews. Observation
   may include the surrounding GitHub resource needed to prove that mutation safe. Repository-wide
   mutation must be requested through an explicit repository-wide mode.
6. **Forget deliberately.** Stop tracking a review only on explicit request or after GitHub and
   fetched trunk prove the work reached it and no other visible stack needs the link.

Most stops and warnings should also name a runnable next step when the right action is clear and
the condition is reasonably likely to occur. This UX requirement never weakens a safety rule.

## State and storage

### Derived from current observations

These facts are re-derived and never need tool-owned durable state:

- local stack topology and parent-child relationships
- each change's current diff base and commit ID
- each PR's desired base branch
- whether a review branch needs to move after a rewrite
- current PR lifecycle, merge-queue presence, and GitHub stack membership

### Stored review state

Tracking stores one pair, keyed by full `change_id`:

- `ReviewIdentity`: GitHub repository owner/name, PR number, and one canonical head owner/ref
- `SubmittedBaseline`: the exact `commit_id` last successfully submitted for that identity

Both records are created, replaced, and removed together. Partial pairs are invalid.

Two named checks recur throughout the policies:

- **identity match**: the live PR's repository, number, and head owner/ref equal the saved
  `ReviewIdentity`
- **snapshot match**: an identity match whose live PR head SHA also equals
  `SubmittedBaseline.commit_id`

Neither check permits mutation alone. Each mutating policy says which other facts it requires.

Commands never replace a tracked missing, closed, moved, or ambiguous PR automatically. A merged
tracked PR directs the user to `sync`; other broken links remain untouched for explicit repair or
cleanup. Once cleanup removes a closed review's tracking, `submit` ignores historical closed or
merged PRs for that branch and creates a fresh PR. An open untracked PR still requires `relink`.

An unreadable, invalid, or unsupported state file blocks commands that load it. The diagnostic
names the exact path and explains how to move it aside before re-adopting reviews through
`checkout` or `relink`.

### Storage locations

User settings live in `jj` config under `[jj-stack]`, following normal user, repository, and
workspace precedence:

```toml
[jj-stack]
branch_prefix = "jj-stack"
reviewers = ["octocat"]
team_reviewers = ["platform"]
labels = ["needs-review"]
```

`submit --reviewers`, `--team-reviewers`, and `--label` override those values for one invocation.
A typo of a known key is rejected with a suggestion; unrelated keys are ignored.

Tracking lives in the user's state directory and is shared by every workspace for the repository.
Nothing is stored in the working tree or `.jj/` internals.

The first successful tracking write creates the repository's tracking file and marks the local
repository as having adopted jj-stack. The file remains when the last review pair is removed, so
adoption outlives individual review stacks. `view`, `list`, and `in-use` never create it.

### Concurrency and interruption

Mutating commands serialize per repository; read-only commands do not. No command saves operation
progress or a replay plan.

After interruption, the next command rereads `jj`, the remote, and GitHub and computes what
remains. Saved identity and baseline are safety observations, never instructions to resume an old
selection.

## Policies

Each durable rule is defined once in this section. Earlier sections introduce concepts and
command purpose; later examples illustrate the rules without redefining them.

### Selection

Commands that inspect reviews use `origin` when it exists, otherwise the sole remote. Several
remotes without `origin` are ambiguous.

Only GitHub's public API is supported. Remote URL hostnames are not validated; the path is
interpreted as a `github.com` owner and repository.

Stack lifecycle commands default to `@` when the working-copy change has a nonblank description
and contents, and to `@-` otherwise. A command that changes review state rejects an explicitly
selected empty or undescribed working-copy change. `view` includes one on the selected path and
warns that it cannot be submitted.
`view` may accept several selectors. An arbitrary revision expression selects the exact revision
it resolves to as the stack head. A bare change ID, including a prefix that identifies one
logical change, or a linked pull request identifies the complete local stack containing that
change. The containing stack ends at the unique visible head descended from the selected change;
several such heads are ambiguous and selection fails closed.

When the selector is a change ID or linked PR, selection prefers the unique mutable copy outside
fetched trunk's first-parent path. A reviewed side parent left by a stack merge remains selectable
until `sync`. Other commands retain their own selection boundary; for example,
`merge --pull-request` merges only through the selected PR, and `relink` requires both the change
and PR.

Four modes deliberately reach beyond one selected stack:

- `sync --all`, which cannot be combined with a selector
- `cleanup` without a selector, which considers every tracked change in the repository
- `cleanup --pull-request <pr>`, which may select one tracked PR whose local change is gone, and
  `cleanup --pull-request orphans`, which selects all such PRs
- `unstack --stack <number>`, which selects one GitHub stack without requiring local tracking

No default invocation mutates reviews beyond the selected stack. A `sync` rebase may propagate to
local descendants under ordinary `jj` rewrite rules. Ambiguous selectors always fail closed.

### Identity and mutation preconditions

Before the first mutation, a command validates the identity on which every planned selected
mutation depends. This prevents a pre-existing mismatch from being discovered only after an
earlier selected review has changed. It does not make a sequence of GitHub requests
transactional: an interruption or a later GitHub rejection can still leave completed lower
steps, which reruns recover through fresh observation.

The command-specific planning requirements are:

- `submit` requires an identity match for every tracked selected change and observes the exact
  remote target of each review branch. Normally that target is the submitted baseline. It may
  already be the change's current commit after an interrupted submit, but only when the identity
  matches and the PR head agrees with that same commit. Any other target stops the command.
- `merge` requires the current local commit and remote review ref both to equal
  `SubmittedBaseline.commit_id`, plus a live snapshot match. Tree or diff equivalence is not
  sufficient.
- `sync --all` requires a snapshot match before retargeting, closing, or cleaning up a review.
- cleanup requires a snapshot match before closing a PR, deleting artifacts, or removing saved
  links.

When the platform supports a conditional write or lease, the mutation is bound to the identity
and version observed while planning. A remote swap, repository retarget, renamed head, moved
branch, missing PR, or replacement PR found during planning fails closed and names `relink` or
`unstack --local`, depending on whether the user needs to repair or forget the saved link.

Only review creation, `relink`, and `checkout` create or replace identity. `unstack --local`
deletes it explicitly. Cleanup is the only operation that deletes identity after checking live
evidence and removing review artifacts; `sync` and `sync --all` invoke that operation rather than
deleting tracking themselves.

Only commands that successfully send or adopt a specific reviewed commit may replace
`SubmittedBaseline` for the same review identity:

- `submit` and `sync`, after a survivor's review update succeeds
- `sync`, when adopting an exact surviving GitHub stack commit
- `sync`, after replacing a GitHub-rebased stack with equivalent commits that retain the original
  change IDs
- `relink`, from the observed remote target
- `checkout`, when adopting an existing review

`merge`, `cleanup`, `view`, and `list` never advance a baseline.

### Submit and branch transport

`submit` publishes only the selected stack, bottom-up. It creates missing PRs, moves existing
review branches, updates PR bases and content, and refreshes GitHub stack membership.

Before any remote mutation, `submit` confirms that the repository is reachable and the GitHub
Stacks API is available, then observes PRs and complete stack membership. An unavailable Stacks
API stops the command before any review branch or pull request changes.

`submit --base B H` publishes only `(B, H]`. `B` must be an ancestor of `H` on the exact
single-parent path and is excluded from every mutation. The base is accepted only when its local
commit, submitted baseline, remote review branch, and live PR head are the same commit; its saved
identity must uniquely match an open live PR. The bottom selected PR targets that review branch.
A one-change selection remains an ordinary PR, while two or more selected PRs form their own
native GitHub stack.

An externally moved or missing base review branch is never overwritten by `submit`. `jj-stack`
cannot repair it automatically: the user must externally restore that exact branch to its saved
immutable submitted commit before retrying. The retry revalidates the base from live
observations.

No boundary is stored or inferred. A later child refresh repeats `--base`; omitting it invokes
ordinary trunk-bounded submit and may include or regroup the parent path. One command never
updates the parent review or another child. The exact named base alone controls the lifecycle:
while its PR remains open, a child refresh repeats the same `--base`; once it lands, even if a
higher change in the parent review survives, `submit --base` stops. After syncing the parent, the
user rebases exactly the child range onto `trunk()`, runs ordinary `submit` without `--base`, and
can then merge that review. `submit` never cascades this transition across related reviews.

When the selected maximal local path no longer matches GitHub's grouping, `submit` first
dissolves the affected GitHub stacks. It may replace one partially selected GitHub stack, which
covers deletion and splitting, or any number of completely selected GitHub stacks, which covers
joining stacks. A rerun observes any work that completed before an interruption and continues
from current state. A review with one remaining active PR is left as an ordinary PR because
GitHub stacks require at least two members.

All selected review branches move in one atomic push. Every update carries the exact target
`jj-stack` observed for that remote ref, including expected absence for a new branch. If any ref
moved, the whole push fails; there is no sequential fallback. `jj-stack` never takes over a
branch for which it has no tracking. The only first-submit recovery is a branch left by an
interrupted push: exactly one managed branch may end in the selected short change ID, and its
commit must carry the full change-ID header.

A PR's desired base is its parent's review branch, or trunk for the bottom change. Position in
the local stack decides that base, not whether the parent's PR remains open. If an intermediate
parent PR is not open, `submit` stops rather than reaching past it.

A topology rewrite counts as a review update even when the tree diff is unchanged. During a
rewrite, `submit` may temporarily retarget selected PRs to prevent GitHub from auto-closing a PR
whose new base contains its head. An interruption may leave bases at their old value, trunk, or
the desired parent; a rerun finishes the update without replacing PRs.

An open PR currently in a merge queue is not updated. `submit` stops the selected stack before
moving any review branch or changing any PR, and tells the user to wait for GitHub to merge it.
This does not restrict commands on independent stacks.

### Merge

`merge` considers a contiguous prefix from the bottom of the selected stack. Candidates must be
open and non-draft. The first draft or closed-unmerged review blocks itself and everything above.
`--pull-request` truncates the candidate prefix at the selected linked PR.

A pull request selector still selects the complete local stack containing that PR; it changes
only the merge boundary. After a direct prefix merge, automatic reconciliation therefore covers
the unmerged changes above that boundary too, including survivor commits GitHub rewrote. An exact
revision expression retains its ordinary exact-head selection and cannot omit active members of
the GitHub stack.

GitHub receives one asynchronous merge request for the selected prefix, whether it contains one
PR or several. A multi-PR request acts on the matching GitHub stack. Every request passes the
exact expected head commit of the top selected PR.

Before the request, `merge` asks whether the trunk branch has a merge queue, using GitHub's merge
queue object or a `MERGE_QUEUE` branch rule. If that lookup fails, `merge` follows the ordinary
direct-merge path and lets the merge request report any policy rejection. It sends the explicit
action `merge_queue` when a queue is found and `direct_merge` otherwise.

A terminal `merged` result means a direct merge completed. `merge` then fetches and runs selected
stack reconciliation before returning. A terminal `enqueued` result means GitHub accepted the
selected PRs into the queue; it is successful but does not imply that trunk changed or that
`sync` should run yet. A rejection changes no local history, and a later command observes
whatever GitHub reports.

Automatic reconciliation identifies the containing stack by the full change ID of the head
resolved before the merge request. It does not reinterpret the original revision expression
after fetching the changed trunk.

GitHub merge success and local reconciliation are separate outcomes. If GitHub completes the
merge but the automatic sync stops, `merge` returns the sync failure status, says that the GitHub
merge must not be retried, and leaves recovery to a later `sync`. That command rereads GitHub,
fetched trunk, the local DAG, and tracking rather than resuming saved operation state.

For a direct merge, the merge method comes from `--method`, otherwise from `merge_method` in
repository configuration, otherwise from the repository's only allowed method. GitHub reports
which methods a repository allows but never which to prefer, so a repository allowing several
with none configured stops rather than choosing one. A configured method the repository does not
allow is refused by name before any request goes out. A merge queue chooses its own method, so the
request omits it; an explicit `--method` produces a warning and is ignored.

Immediately before merging or enqueueing an ordinary one-PR review, `jj-stack` retargets the
candidate to trunk.

`merge` does not compare trunk commits before planning. Trunk advancing under a reviewed stack is
routine, and GitHub merges a pull request whose base is behind unless it conflicts, so whether the
merge is possible is GitHub's answer to give. The single-PR candidate is retargeted to the trunk
branch by name and sent with its expected head commit, so that mutation does not depend on which
commit trunk points at. A one-PR prefix selected from a larger GitHub stack remains a stack merge
and is not retargeted by this rule.

A reviewed change GitHub already merged is still a stop, decided from the pull request's own
reported state rather than from trunk position. That boundary names `sync`, because the local
stack holds a copy of work already on trunk.

### Repository policy

A merge initiated through GitHub's UI, auto-merge, or another client is supported. Rebasing a
complete native GitHub stack through GitHub's UI is also supported. A later `sync` reconciles
either result under the rules below.

`jj-stack` does not duplicate repository policy. Apart from choosing direct merge or queue
routing for the trunk branch, it does not preflight approvals, checks, conflicts, or auto-merge
state across the repository. GitHub applies those rules to the requested GitHub stack or
single-PR mutation, and `jj-stack` reports the result.

A rejection therefore has to explain itself. Because conflicts reach the user here rather than
through a local preflight, a rejected merge names the way out: rebase onto trunk, resolve, and
submit again for a conflict; fix the check or rule on GitHub otherwise.

### Trunk evidence and sync

Two observations prove that reviewed work reached trunk. GitHub reporting a pull request as
merged is not one of them, because it says nothing about the trunk this repository fetched:

- **Exact submitted commit on trunk**: the baseline is an ancestor of fetched trunk and the live
  PR is a snapshot match. A PR belonging to a GitHub stack must also report merged before `sync`
  may act on it.
- **Selected PR's rewritten merge result on trunk**: the saved PR is an identity match, reports
  merged, still reports the submitted head, and reports a merge-result commit that is an ancestor
  of fetched trunk. This covers squash and rebase results.

`sync` may use either proof. `sync --all` uses each rewritten merge result only to select and
reconcile the local stack containing that review; it does not apply one review's evidence to a
different stack. If no local copy remains, it uses that evidence only for ordinary cleanup.

A PR merely reporting merged, or a merge result no longer reachable from fetched trunk,
permits no change. Local revisions, identity, and baseline remain untouched until a later sync can
prove the result on fetched trunk.

When an unmerged local change sits below a reviewed change whose submitted work is proven on
fetched trunk, `sync` stops without mutation. Rebasing would silently decide whether that local
change belongs before or after the merged work. The diagnostic names the changes and the exact
submitted, local, and fetched-trunk commits, then gives a `jj log` command for inspecting both
histories. The user chooses the intended order with ordinary `jj`. Afterward they inspect the
remaining local reviews, sync a remaining mutable reviewed head, or run cleanup when no reviewed
local copy remains.

Here unpublished local work means a mutable revision whose commit is neither its submitted
baseline nor an exact GitHub stack head that this run may adopt.

`sync` reconciles the unmerged suffix only when:

- rewriting it would not discard unpublished local work
- no surviving change is divergent
- no unreviewed change sits between reviewed survivors

If any selected open PR is still in a merge queue, `sync` leaves the selected stack unchanged.
Once GitHub no longer reports it queued, ordinary trunk evidence determines whether `sync`
reconciles merged work or has nothing to do.

It rebases surviving changes onto fetched trunk even when they contain conflicts. If a reviewed
survivor remains conflicted, the local rebase stays in place but its review is not updated. The
user resolves the conflict with `jj` and runs `submit` for the remaining stack.

If a workspace directly has an obsolete merged change checked out, `sync` does not remove that
change. Its diagnostic identifies the workspace and gives commands to move it to trunk or forget
it and move its directory to the trash. A workspace on a surviving child does not block its
ordinary rebase.

Rewriting a selected revision may also rebase its local descendants under ordinary `jj` rules. If
another local path still depends on a merged revision after that rewrite, `sync` leaves the
revision and its tracking in place and names each other stack that still needs `sync`. It never
updates reviews outside the selected chain.

After survivor updates succeed, `sync` invokes cleanup for merged reviews that no local path still
needs. Cleanup removes each eligible review branch and managed overview comment before it removes
the corresponding tracking. A blocked or failed cleanup leaves tracking for a retry. A failure
after local convergence leaves completed work in place; later commands observe the current DAG,
tracking, and GitHub state instead of replaying saved operation state. `sync` never rebases merely
because trunk advanced; ordinary `jj rebase` owns that workflow. Its output describes
reconciliation and cleanup, not submission, including when no reviews survive.

GitHub preserves `jj`'s `change-id` commit header through rebase merges of PRs, but not squash
merges. A matching full change ID on fetched trunk identifies the successor rather than
an arbitrary visible side copy. When fetched trunk has no matching change ID, `sync` retires the
old local change without relabeling that commit or storing an alias.

When a GitHub stack merge rewrites active members above the merged prefix, `sync` adopts the exact
commits GitHub reports rather than replaying equivalent diffs. It accepts those heads and bases
only while a merged tracked member of the same GitHub stack proves the transition.

GitHub's native stack rebase instead rewrites every active member and removes `jj`'s change-ID
commit headers. With no merged member, those remote commits cannot become the identity of the
local changes. `sync` recognizes this result only when all of these observations agree:

- the GitHub stack contains exactly the selected tracked reviews, in their local parent order
- every PR still uses its saved head branch and the expected base branch
- every PR head and review branch moved from its submitted baseline to the same reported commit
- the reported commits form one first-parent chain rooted at fetched trunk
- the selected local changes have no divergent revisions

`sync` then computes a rebase of the original local changes without first changing the local DAG.
The computed change IDs must remain the selected change IDs, conflicts are rejected, and each
computed commit tree must exactly equal the corresponding GitHub commit tree. This comparison is
also the recovery proof when a previous run integrated the local rebase but failed before moving
the review branches.

After the proof succeeds, `sync` integrates the local rebase, atomically replaces every rewritten
review branch using the observed GitHub heads as exact leases, and records the resulting local
commits as the submitted baselines. A changed lease leaves the local rebase in place and advances
no baseline; a retry proves its trees against the freshly observed stack. No alias from a GitHub
commit to a local change ID is stored.

### GitHub stack membership

`merge`, `sync`, and `unstack` require every active member of the one GitHub stack they touch to
belong to the selected local parent chain. Cleanup instead checks each candidate and never
deletes a branch needed by an active GitHub stack member.

`submit` reconciles GitHub grouping from the selected local path. It may dissolve any number of
GitHub stacks whose active members are all selected. It may also dissolve one partially selected
GitHub stack when the selection is a maximal local path and touches no other GitHub stack. A
non-maximal selection could silently truncate a still-valid stack, so it stops before mutation.
Likewise, a selection that partly overlaps one GitHub stack while including any previously
submitted review outside that resource stops; the user submits the source path first, then the
destination path.

Merged members do not have to be selected. If selected PRs appear only as history, one matching
GitHub stack may be observed without mutation; more than one is ambiguous and stops the command.

For strict commands, an active unselected member or two active GitHub stacks in one selection
fails before mutation. The diagnostic names the exact `jj-stack unstack --stack <number>`
command when removing the grouping can unblock the operation.

Changing the base of an active GitHub stack member requires dissolving that GitHub stack first
because GitHub offers no single-member removal. `jj-stack` asks GitHub to dissolve the exact
observed resource. If GitHub retains a queued or otherwise locked active member, the operation
stops before changing any branch or base. Historical merged members may remain in the resource;
they do not block the mutation because they are no longer active.

### Derived artifacts

PR titles, bodies, and the stack overview comment are derived on every submit and never determine
topology; see [pull request descriptions](../reference/descriptions.md).

The managed overview comment is rediscovered by an unambiguous body marker, never a stored
comment ID. Ambiguous matches are left untouched. A one-PR review has no overview comment. New
PRs are created in the requested draft state. Existing PRs become draft only with `--draft=all`
and become ready only with `--open`; plain `submit --draft` never unpublishes an existing PR.
With `--edit`, GitHub's current state and those command-wide defaults populate one editable draft
choice per change. The validated document then determines each selected PR's draft state without
adding local state.

`--reviewers` and `--team-reviewers` request the named reviewers even when a PR is otherwise
unchanged and never remove omitted reviewers. `--re-request` acts on an otherwise unchanged PR,
asking again only for users whose latest opinionated review approved or requested changes. It
adds requests and never cancels a pending one. Labels are also additive; omitted labels are never
removed.

### Unstack and cleanup

`unstack` removes one exact GitHub stack grouping and leaves every pull request, review branch,
overview comment, and tracking record unchanged. With `--stack <number>`, GitHub is the source of
the selected resource and no local tracking is required. Otherwise the selected local review
stack must identify one coherent GitHub stack. Rerunning it after the grouping is gone is safe.

`unstack --local` removes local tracking for the selected local review stack only. It never
touches GitHub or local history and is the one explicit way to forget a review without trunk
evidence.

Closing pull requests through GitHub's UI or `gh pr close` is supported. It leaves local tracking
in place, so `submit` does not silently reuse a closed review and `cleanup` can still prove which
branches and comments belong to it. Starting reviews over means closing the old pull requests,
running selected cleanup, and then submitting again.

`cleanup --pull-request <pr> --close` and `cleanup --pull-request orphans --close` combine closure
and cleanup for an explicit saved selection. The flag is invalid without `--pull-request`.
Identity, snapshot, review-branch ownership, open dependents, GitHub stack membership, and the
managed overview comment are all checked before closing an open PR. A PR already closed or merged
skips closure and follows ordinary cleanup. A closure failure stops later selected mutations; a
rerun observes the current PR state.

Cleanup acts only on one complete identity/baseline pair, whether it runs directly or at the end
of `sync`. It may remove the managed overview comment, the exact saved review ref only while it
still points to the expected commit, and the two records.

A pair is eligible only when:

- GitHub reports the exact saved PR closed or merged
- for a merged PR, no visible mutable local copy still needs `sync`
- no open PR in the same repository uses the saved head ref as its base
- no active member of a GitHub stack still needs the branch

Local descendants do not substitute for the open-PR base check. A visible mutable copy of merged
work is evidence for `sync`, not deletion of a GitHub branch or tracking.

Identity and baseline are removed only after artifact cleanup succeeds. A PR that cannot be
inspected is skipped. Once mutation starts, a failure stops cleanup and leaves later records for
a rerun.

### Adoption and repair

`checkout --pull-request` treats the selected PR as the head of the remote chain to adopt and
edit. `--revset` selects an exact local head. `--pick` combines locally tracked paths with active
GitHub stack resources, showing each GitHub stack's number, top active PR, base, size, status, and
whether it is already local. Choosing a GitHub-only or partially tracked stack passes its top
active PR through the same adoption path as `--pull-request`; the picker does not create another
tracking path.

Before choosing a local or fetched snapshot, `checkout` reads each PR head's change ID. If that
change ID already exists locally at another commit, it stops and names `relink` rather than
choosing between the reviewed snapshot and the local rewrite. When the selected PR's exact head
commit is absent locally, it fetches ordinary remote state and imports the selected review
through a temporary ref. It validates the complete selected stack and saves any new tracking
before it runs `jj edit` on the exact head commit it observed. If the workspace move fails after
adoption, a rerun observes the saved tracking and retries the move. The command does not rebase
review changes, restack descendants, or mutate PRs, and it leaves no review bookmarks behind.

`relink` explicitly replaces uncertain tracking for one change. It verifies the known PR and
same-repository head branch, then saves the identity and exact observed remote target as one pair.
Replacing the stale baseline lets a later `submit` update the known review rather than reject the
branch as foreign.

`doctor` observes setup, GitHub Stacks API availability, and local leftovers from interrupted
`checkout` or `sync`. It changes nothing without `--fix`; currently the only automatic repair is
restoring the reserved review-branch exclusion in remote fetch configuration. It never mutates
GitHub.

### Inspection

`in-use` is a silent local predicate. It exits 0 when the repository has a valid tracking file
and 1 when the file is absent. An invalid file or failure to locate a jj repository is an error,
not a negative result, and exits 11.

`view` and `list` are read-only. For local stack rows, both observe saved review branches directly
on the remote and ask GitHub for current PR state without fetching. Orphan rows in `list` show
saved identity only; they do not claim to report the PR's live state.

Both commands project paths from the local `jj` DAG. One projected path may therefore show
changes that explicit submit boundaries placed in several native GitHub reviews. Inspection does
not segment the path by GitHub resource or infer an omitted submit boundary.

Both report whether an open PR currently has a merge-queue entry. Queue presence is a transient
GitHub observation, not saved tracking; position and intermediate queue phases are not modeled.

Neither command guesses. A change with no saved review identity is reported as not submitted,
even if a PR happens to use the branch name that change would generate.

Inspection tolerates history exposed by fetch rather than immediately declaring the stack broken.
`view` walks past immutable or divergent side copies of merged changes. A merged PR still in the
local stack becomes a `cleanup needed` row naming `sync`. Only when no supported
linear walk remains does `view` stop with a targeted diagnostic.

Local review eligibility never prevents an otherwise resolvable `view` report. Empty or
undescribed working-copy changes, divergent changes, conflicts, and merge commits are shown with
warnings that explain which mutation remains blocked. A merge is projected through its first
parent, which the warning states explicitly. These warnings do not make an otherwise complete
report incomplete; divergence and unresolved remote observations retain their existing
incomplete-report rules. Mutation commands continue to reject unsupported selections.

A per-change lookup failure marks only that row unresolved and produces an incomplete report. A
failure before any rows can be built returns its own exit code. `list` includes orphaned PRs as
separate rows.

`view` and `list` decide most incompleteness from one shared per-change rule: an unmerged
divergent change, an ambiguous PR, a failed PR lookup, or a saved PR link the branch no longer
resolves.
When several local changes claim one saved branch, `list` warns, skips live inspection for that
branch, and exits 10 rather than assigning its remote or PR state to the wrong change.

`view` and `submit` render stack rows through the user's `jj log` formatting. `--json`
follows [`docs/json-output.schema.json`](../json-output.schema.json) and exposes no cache state,
raw remote targets, or tracking records.

### Rewrite behavior

Most rewrites follow directly from stable change IDs and DAG-derived topology. Two cases need
explicit rules:

- **Abandon**: the change leaves every current local stack and descendants attach to its parent.
  Its PR becomes orphaned. Surviving stacks never close, reuse, or retarget it. Explicit closure
  and cleanup use `cleanup --pull-request <pr> --close`.
- **Split**: new logical changes get new change IDs and normally new PRs. The change retaining the
  original change ID retains its PR.

#### Cross-stack rewrites

When a rewrite moves changes between local stacks, identity still follows full `change_id`, each
stack command still updates reviews for one selected chain, and ambiguous linkage still fails
closed. Local `jj` rewrites may propagate to descendants on another path. Reviews on that path
wait for their own explicit commands.

- **Move changes between reviews**: submit the source path first, then the destination path. The
  first submit dissolves grouping that still includes the moved change; the second joins the
  complete destination reviews. Moved changes retain their PRs and recalculate bases from their
  new parents. A trunk-based result uses ordinary `submit`; a result whose lower bound is reviewed
  change `B` uses `submit --base B H`. Destination-first submission stops before mutation.
- **Split one stack into several**: each maximal linear path appears separately in repository
  inventory. The paths may contain the same observed reviewed ancestors; tracking annotates those
  paths but does not put a shared PR in several GitHub stacks. The shared fork remains in its
  parent review; each outgoing child is submitted as `(fork, head]` with explicit `--base`.
  Submitting the first bounded path dissolves the old grouping and rebuilds that path; each other
  path waits for its own submit.
- **Join several stacks into one**: submitting the resulting chain dissolves every completely
  selected GitHub stack and creates the joined grouping. It reuses reviews by change ID,
  recalculates every base, and produces one overview comment on the new head.

Stacks not yet resubmitted may still show old overview comments. That is expected:
`submit` does not mutate stacks outside its selection. `list` identifies stale reviews across the
repository by comparing each baseline with the current change and naming the stack to refresh.
Close orphaned PRs and remove their leftovers with `cleanup --pull-request <pr> --close`.

## CLI contract

Built-in `--help` and the user guide own exact parser syntax and aliases. This specification owns
enduring selection rules, command effects, and exit meanings.

`help --all` adds advanced commands and hidden global options to ordinary top-level help.
`help --all-in-one` emits one deterministic Markdown document containing the detailed help for
every command. It adds semantic HTML classes for commands, options, and metavariables so a
documentation site can style the syntax without parsing terminal output.

Running the executable without a subcommand is equivalent to `view` without arguments.

### Exit codes

Process exit codes are part of the CLI contract. Where a meaning overlaps with the `gh stack`
extension, the code matches. Codes 7-9 remain reserved because their `gh stack` meanings have no
`jj-stack` equivalent.

- `0` — success
- `1` — `in-use` found no local adoption; otherwise any other failure, including a lifecycle
  command blocked before completion
- `2` — the selection does not form a supported local review stack
- `3` — unresolved conflicts block the requested operation
- `4` — GitHub authentication, network, or API failure
- `5` — invalid command-line arguments
- `6` — a selector matched more than one target
- `10` — `view` or `list` printed an incomplete report
- `11` — `in-use` could not determine its result
- `130` — interrupted

The user-facing table lives in
[docs/reference/automation.md](../reference/automation.md#exit-codes).

## Current scope

Supported:

- one review remote and one repository on GitHub's public API per invocation; remote URL hosts are
  not validated
- linear local review stacks
- visible mutable review changes
- one PR per review change
- GitHub stacks for every multi-PR review

Unsupported:

- stacked reviews crossing repositories or remotes
- nonlinear local review stacks

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
