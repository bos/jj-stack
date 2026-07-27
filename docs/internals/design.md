# Stacked GitHub review from `jj`: design

This is the canonical product specification for `jj-stack`.
Implementation structure belongs in `implementation-strategy.md`; evidence policy belongs in the
testing and review documents; deferred questions belong in `backlog.md`.

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
and the exact commit last sent for review. Everything else is observed or derived.

Three goals shape the design beyond that model: stacked GitHub PRs should feel native in a `jj`
workflow, the tool should be easy to use, and review branch names should stay stable across
rewrite-heavy review.

## From local changes to pull requests

Suppose the selected local history is:

```text
trunk() <- A <- B <- C
```

`A`, `B`, and `C` are review changes, and `C` is the selected head. On GitHub they become:

```text
PR for C: head review/C, base review/B
PR for B: head review/B, base review/A
PR for A: head review/A, base trunk
```

The actual branch names include a subject slug and change-ID suffix, but those names are only
GitHub transport. The `jj` parent relation determines the stack and each PR's base.

The normal lifecycle is:

1. Use `jj` to create, reorder, split, squash, or rebase local changes.
2. Use `jj-stack view` to inspect the stack, and `jj-stack list` to see all stacks.
3. Use `jj-stack submit` to create or refresh the PRs for that selected stack.
4. After another local rewrite, run `submit` again; existing reviews follow their change IDs.
5. Use `jj-stack merge` to ask GitHub to merge a reviewed prefix from the bottom.
6. Run `jj-stack sync` for that selected stack. It fetches GitHub's result, then reconciles the
   remaining local changes and reviews with what landed on trunk.

## Core concepts

### Review change

A review change is one visible mutable `jj` change, identified by full `change_id`. A `change_id`
is the durable identity of a logical change across rewrites; a Git commit ID is not. A review
change's current commit ID, remote branch name, and diff base are not part of its identity.

"Visible mutable" follows `jj`'s own revsets:

- visible: the commit is in `visible()`, not a hidden predecessor
- mutable: the commit is in `mutable()`, with immutability defined by the repository's
  `immutable_heads()`

By default, `trunk()`, tags, and untracked remote bookmarks define immutable history. If the
repository customizes `immutable_heads()`, `jj-stack` honors that rather than maintaining a
competing notion of what is safe to review or rewrite.

A commit meeting those conditions is *reviewable*: eligible to become a review change. Two extra
eligibility rules apply to the working copy. An empty working-copy commit is not reviewable, and
an undescribed working-copy commit cannot be selected for review until the user describes it.

### Local review stack

A local review stack is the chain of single-parent commits from a selected head back to the
nearest commit also reachable from `trunk()`. That commit is the stack's base and is not itself
part of the stack.

`jj-stack` supports only linear stacks, so the walk follows each commit's sole parent. A merge
commit inside the selected chain is rejected, as is a divergent review change. Unresolved
conflicts are a separate matter: they do not break the shape, so `view` and `list` report a
conflicted change, while `submit`, `merge`, and `sync` refuse to act on one.

Commands validate only the selected chain. Other visible children elsewhere in the DAG are not
an error. If an ancestor on the selected chain has another reviewable child, that child and its
descendants are out of scope unless the command explicitly selects them.

A rebase merge preserves `jj`'s change ID, so once the result is fetched, the landed commit on
trunk and the superseded local commit are two visible copies of one change ID: the local copy is
divergent and the trunk copy is immutable. Both are recovery context, not review changes to
publish.

### Tracking

`jj-stack` remembers two facts about each change it has published:

- the **review identity**: which GitHub PR and which review branch belong to that change
- the **submitted baseline**: the exact commit last successfully sent for review

A change is **tracked** once a review identity exists for it, and **untracked** otherwise. A
predicted branch name, or a PR that happens to use one, does not make a change tracked; only a
saved identity does.

Tracking records which review a change owns, which prevents mutating the wrong one. It does not
show on its own that a mutation is safe.

### Review branches and PR bases

GitHub review is branch-based: every PR needs one head branch and one base branch. The `jj` DAG
supplies neither, so `jj-stack` maintains remote branches purely as transport.

Each tracked review change has exactly one remote Git branch used as its GitHub PR head. These
branches live on the remote only and remain outside the local `jj` view.

The initial name is:

```text
review/<slug-from-subject>-<change_id.short(8)>
```

For example:

```text
review/add-cache-index-ypvmkkuo
```

The slug is lowercase ASCII derived from the first description line. The change-ID suffix ties
the branch to the logical change. In the extremely unlikely case that two selected changes
resolve to the same name, `submit` stops.

The subject is only used once, when creating the initial name for a branch. Once a review is
tracked, the branch name stays stable. Commands do not rename or replace it because the
description changed.

The GitHub base branch for a review change is:

- the parent review change's remote branch when the parent is in the local review stack
- otherwise the trunk branch

`trunk()` defines the lower bound of a stack without specifying a GitHub branch name. For GitHub
operations, the tool resolves it to one concrete remote bookmark on the selected remote, such as
`main@origin`. That branch must be either GitHub's reported default branch or an unambiguous
bookmark on that remote whose target is `trunk()`. If `trunk()` falls back to `root()` or cannot
be mapped to exactly one such bookmark, `submit` stops rather than guessing.

### The reserved `review/` branch namespace

`jj-stack` reserves the fixed `review/` namespace for its managed branches. Ordinary `jj`
bookmarks outside that namespace behave normally.

The namespace has to stay out of the local `jj` view. `jj`'s default `immutable_heads()` counts
untracked remote bookmarks as immutable, so an ordinary fetch of `review/*` would make every
review branch target immutable, which takes the changes it points at out of review. `jj-stack`
therefore excludes the whole namespace from the remote's fetch configuration.

Before fetching or mutating reviews, `jj-stack` stops if a `review/` bookmark has reached the
local `jj` view and names the repair. Plain `view` and `list` do not run this preflight.

### GitHub stack objects

A **GitHub stack** is GitHub's optional server-side object: an ordered group of pull requests.
This is distinct from the local review stack derived from the `jj` DAG.

When the repository supports GitHub stacks, `jj-stack` registers the selected PRs in that object.
When it does not, the PRs remain ordinary stacked PRs and `jj-stack` supplies navigation through
managed comments. Capability detection, not GitHub's current rollout label, determines which
behavior applies.

GitHub reports merged members as a historical bottom prefix. This document calls the remaining
members **active members**, regardless of whether an individual PR is open, draft, or closed.

### Workspaces

Several `jj` workspaces can share one repository, and each has its own working-copy commit.
Repository-wide discovery omits all working-copy commits so the inventory does not depend on
which workspace ran it. A stack command defaults to `@` when that workspace's working-copy
change is described and nonempty, and to `@-` otherwise.

If `jj` reports that a workspace is stale, the command stops and tells the user to run
`jj workspace update-stale`.

## Commands and lifecycle

This is a map of command purpose and scope. Later policy sections define exact eligibility,
evidence, and mutation rules.

- **`view`** inspects one or more selected stacks and reports local, remote-branch, and GitHub
  state. With no selector it uses the default under [Selection](#selection).
- **`list`** reports the repository-wide inventory of local stacks and orphaned tracked reviews.
- **`submit`** publishes the selected stack. It is the only command that creates a PR or
  publishes a never-submitted change.
- **`sync`** on a selected stack (called **selected `sync`** below) reconciles that stack after
  reviewed work lands. It may rewrite surviving local changes, update their existing reviews, and
  retire tracking for landed changes. It never creates a PR.
- **`sync --all`** performs weaker repository-wide reconciliation. It may retarget and close
  reviews proven landed by exact submitted-commit evidence, but never rewrites local stacks or
  submits work. A tracking record or GitHub review that cannot be read does not block independent
  candidates.
- **`merge`** is the only command that asks GitHub to merge. It never pushes trunk or rewrites
  local history.
- **`unstack`** ends review by closing tracked open PRs. `--cleanup` also removes eligible
  artifacts; `--local` only forgets local tracking.
- **`cleanup`** removes eligible branches, managed comments, and tracking left by closed or
  merged reviews across the repository. It is optional housekeeping, not part of correctness or
  local-history recovery.
- **`checkout`** adopts review state already on GitHub. It sets up tracking but does not move the
  workspace or rewrite local commits.
- **`relink`** attaches one known PR and same-repository head branch to one selected change when
  the user knows the identity but the tool cannot prove it.
- **`doctor`** reports setup, connectivity, and observable leftovers from interrupted local
  operations. `--fix` applies only the local repairs it names.
- **`completion`** prints shell completion scripts and inspects nothing.

There is no standalone `rebase` command; `jj` owns general descendant rewrites.

`sync`, `merge`, and `checkout --fetch` run `jj git fetch` themselves before they act. No other
command fetches, so the user runs `jj git fetch` when local trunk is stale. **Fetched trunk**
below always means `trunk()` as evaluated after the running command's own fetch.

## Sources of truth

Three sources answer questions about a review, each for a different domain:

1. The **`jj` DAG** determines which local changes exist, how they are related, and what they
   contain.
2. **GitHub** reports PR existence, lifecycle, reviews, GitHub stack membership, and merge
   results. Whether work actually landed is proven separately by ancestry from fetched trunk.
3. **Local tracking** records only the review identity and submitted baseline of each change. It
   prevents mutation of the wrong review but cannot make a mutation safe on its own.

## Safety rules, in priority order

Within the supported scope, these rules are ordered; a lower rule never weakens a higher one.

1. **Never lose work.** `jj` can undo almost any local mistake; GitHub cannot undo every remote
   mutation. Protect local commits first and treat destructive GitHub operations explicitly.
2. **Check the target.** Before changing a branch, PR, GitHub stack, or repository, confirm it is
   the intended one and use a guard that fails if it moves underneath the command.
3. **Never guess.** Ambiguous linkage stops the command. Never guess which PR belongs to a change
   or silently adopt one that appeared in place of another.
4. **Merge what was reviewed.** Merge only the exact commit submitted for review, and re-confirm
   PR identity and head immediately before every irreversible merge step.
5. **Stay in the selected stack.** Stack-scoped commands mutate only selected reviews. Observation
   may include the surrounding GitHub resource needed to prove that mutation safe. Repository-wide
   mutation must be requested through an explicit repository-wide mode.
6. **Forget deliberately.** Stop tracking a review only on explicit request or after GitHub and
   fetched trunk prove that the work landed and no other visible stack needs the link.

Most stops and warnings should also name a runnable next step when the right action is clear and
the condition is reasonably likely to occur. This UX requirement never weakens a safety rule.

## State and storage

### Derived from current observations

These facts are re-derived and never need tool-owned durable state:

- local stack topology and parent-child relationships
- each change's current diff base and commit ID
- each PR's desired base branch
- whether a review branch needs to move after a rewrite
- current PR lifecycle and GitHub stack membership

### Stored review state

Tracking holds two record types keyed by full `change_id`:

- `ReviewIdentity`: GitHub repository owner/name, PR number, and one canonical head owner/ref
- `SubmittedBaseline`: the exact `commit_id` last successfully submitted for that identity

A tracked change always has a `ReviewIdentity`; a complete submitted review also has a
`SubmittedBaseline`.

Two named checks recur throughout the policies:

- **identity match**: the live PR's repository, number, and head owner/ref equal the saved
  `ReviewIdentity`
- **snapshot match**: an identity match whose live PR head SHA also equals
  `SubmittedBaseline.commit_id`

Neither check permits mutation alone. Each mutating policy says which other facts must be read
and when they must be rechecked.

Commands never replace a missing, closed, moved, or ambiguous PR automatically. They leave the
tracking untouched and direct the user to `relink`, or to `unstack --cleanup` followed by a fresh
`submit`.

Recording a submitted commit cannot replace PR identity: the write fails if the identity changed
underneath it. A missing or invalid per-change record is isolated and reported with `relink` as
its repair.

An unreadable or unsupported top-level state file blocks commands that load it. The diagnostic
names the exact path and explains how to move it aside before re-adopting reviews through
`checkout` or `relink`.

The repository-pair capability cache for GitHub stacks is separate from per-change records. The
first command that needs the fact asks GitHub and persists the result.

### Storage locations

User settings live in `jj` config under `[jj-stack]`, following normal user, repository, and
workspace precedence:

```toml
[jj-stack]
reviewers = ["octocat"]
team_reviewers = ["platform"]
labels = ["needs-review"]
```

`submit --reviewers`, `--team-reviewers`, and `--label` override those values for one invocation.
A typo of a known key is rejected with a suggestion; unrelated keys are ignored.

Tracking lives in the user's state directory and is shared by every workspace for the repository.
Nothing is stored in the working tree or `.jj/` internals.

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

Remote hostnames are deliberately ignored, without safety checks. Only `github.com` is
supported.

Stack lifecycle commands default to `@` when the working-copy change has a nonblank description
and contents, and to `@-` otherwise. Explicit empty or undescribed working-copy selections fail.
`view` may accept several selectors. `relink` requires both the change and PR.

Three modes deliberately reach beyond one selected stack:

- `sync --all`, which cannot be combined with a selector
- `cleanup`, which considers every tracked change in the repository
- `unstack --cleanup --pull-request <pr>`, which may select one tracked PR whose local change is
  gone, and `unstack --cleanup --pull-request orphans`, which selects all such PRs

No default invocation mutates beyond the selected stack. Ambiguous selectors always fail closed.

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
- `sync --all` requires a snapshot match before retargeting, closing, or retiring a review.
- cleanup requires a snapshot match before deleting artifacts or retiring tracking.

Immediately before each irreversible action, the command rereads its required facts. A remote
swap, repository retarget, renamed head, moved branch, missing PR, or replacement PR fails closed
and names `relink` or `unstack --cleanup` followed by a fresh `submit`.

Only review creation, `relink`, and `checkout` create or replace identity. `unstack --local`
deletes it explicitly; selected `sync`, `sync --all`, and cleanup retire it from evidence.

Only commands that successfully send or adopt a specific reviewed commit may replace
`SubmittedBaseline` for the same review identity:

- `submit` and selected `sync`, after a survivor's review update succeeds
- selected `sync`, when adopting an exact surviving GitHub stack commit
- `relink`, from the observed remote target
- `checkout`, when adopting an existing review

`merge`, `sync --all`, `cleanup`, `view`, and `list` never advance a baseline.

### Submit and branch transport

`submit` publishes only the selected stack, bottom-up. It creates missing PRs, moves existing
review branches, updates PR bases and content, and refreshes GitHub stack membership or managed
navigation comments.

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

After all planned work, `submit` refetches every selected PR that was open at the start. If one is
now closed or missing, it fails loudly and tells the user how to inspect or reopen it. This
post-check detects GitHub-side changes that the pre-push model could not prevent; it never hides
them by silently replacing tracking.

### Merge

`merge` considers a contiguous prefix from the bottom of the selected stack. Candidates must be
open and non-draft. The first draft or closed-unmerged review blocks itself and everything above.
`--pull-request` truncates the candidate prefix at the selected linked PR.

When the candidates belong to one GitHub stack, GitHub receives one asynchronous group request
for the selected prefix. Only a terminal merged result is success; rejection changes no local
history, and a later `sync` observes whatever GitHub reports.

Without a GitHub stack, PRs merge bottom-up through the ordinary API. A rejection stops the
sequence, leaving any lower PRs already merged. Rebase merge is refused for more than one
ordinary PR because the first rewrite invalidates the reviewed commit identity of the rest.

Immediately before each ordinary merge, `jj-stack` retargets the candidate to trunk and passes
the exact expected head commit.

### Repository policy

A merge initiated through GitHub's UI, auto-merge, or another client is supported. A later
selected `sync` reconciles it under the landed-evidence rules below.

`jj-stack` does not duplicate repository policy. It does not preflight approvals, checks,
conflicts, merge queues, or auto-merge state across the repository. GitHub applies those rules to
the requested GitHub stack or ordinary PR mutation, and `jj-stack` reports the result.

### Landed evidence and sync

Two observations prove that reviewed work landed:

- **Exact submitted commit on trunk**: the baseline is an ancestor of fetched trunk and the live
  PR is a snapshot match. A PR belonging to a GitHub stack must also report merged before selected
  `sync` may act on it.
- **Selected PR's rewritten merge result on trunk**: the saved PR is an identity match, reports
  merged, still reports the submitted head, and reports a merge-result commit that is an ancestor
  of fetched trunk. This covers squash and rebase results.

Selected `sync` may use either proof. `sync --all` may use only exact submitted-commit evidence
because a rewritten merge result is selected-stack evidence and cannot support repository-wide
local change.

A PR merely reporting merged, or a merge result no longer reachable from fetched trunk,
permits no change. Local revisions, identity, and baseline remain untouched until a later sync can
prove the result on fetched trunk.

Here unpublished local work means a mutable revision whose commit is neither its submitted
baseline nor an exact GitHub stack head that this run may adopt.

Selected `sync` reconciles the unmerged suffix only when:

- rewriting it would not discard unpublished local work
- the remainder is linear and conflict-free
- no unreviewed change sits between reviewed survivors
- no other visible stack depends on a landed revision being retired

It rebases surviving changes onto fetched trunk, updates existing reviews, and removes tracking
for landed changes only after survivor updates succeed. It never rebases merely because trunk
advanced; ordinary `jj rebase` owns that workflow.

GitHub preserves `jj`'s `change-id` commit header through rebase merges of PRs, but not squash
merges. A matching full change ID on fetched trunk identifies the landed successor rather than
an arbitrary visible side copy. When fetched trunk has no matching change ID, `sync` retires the
old local change without relabeling the landed commit or storing an alias.

When a GitHub stack merge rewrites active members above the merged prefix, selected `sync` adopts
the exact commits GitHub reports rather than replaying equivalent diffs. It accepts those heads
and bases only while a merged tracked member of the same GitHub stack proves the transition.

### GitHub stack membership

Every active member of a GitHub stack touched by a selected mutation must belong to the selected
local parent chain. A selected operation may touch at most one GitHub stack that still has active
members to change.

Merged members do not have to be selected. If selected PRs appear only as history, one matching
GitHub stack may be observed without mutation; more than one is ambiguous and stops the command.

An active unselected member, two active GitHub stacks in one selection, or membership that
changes during the command fails before branch or PR mutation. The diagnostic names the exact
`gh stack unstack <number>` command when dissolution can unblock the operation.

`submit`, `merge`, selected `sync`, and `unstack` use this rule. Cleanup instead checks each
candidate and never deletes a branch needed by an active GitHub stack member.

Changing the base of an active GitHub stack member requires dissolving the active part of that
GitHub stack first because GitHub offers no single-member removal. `jj-stack` asks GitHub to
dissolve the exact observed resource. If GitHub retains a queued, auto-merge, or otherwise locked
active member, the operation stops before changing any branch or base.

### Derived artifacts

PR titles, bodies, navigation comments, and the stack overview comment are derived on every
submit and never determine topology; see [description helpers](../description-helpers.md).

Managed comments are rediscovered by an unambiguous body marker, never a stored comment ID.
Ambiguous matches are left untouched. Navigation comments exist only where GitHub stack objects
are unavailable. New PRs are created in the requested draft state. Existing PRs become draft
only with `--draft=all` and become ready only with `--open`; plain `submit --draft` never
unpublishes an existing PR.

`--reviewers` and `--team-reviewers` request the named reviewers even when a PR is otherwise
unchanged and never remove omitted reviewers. `--re-request` acts on an otherwise unchanged PR,
asking again only for users whose latest opinionated review approved or requested changes. It
adds requests and never cancels a pending one. Labels are also additive; omitted labels are never
removed.

### Unstack and cleanup

`unstack` closes the selected open PRs but retains identity and baseline so later cleanup can
prove what it is deleting. With `--cleanup`, it also removes every eligible artifact. Closing and
cleaning a stack is the supported way to start its reviews over; a later `submit` creates fresh
PRs under the ordinary generated names.

`unstack --local` removes local tracking only. It never touches GitHub or local history and is the
one explicit way to forget a review without landed evidence. Rerunning any `unstack` mode is safe.

Cleanup acts only on one complete identity/baseline pair. It may remove managed comments, the
exact saved review ref only while it still points to the expected commit, and the two records.

A pair is eligible only when:

- GitHub reports the exact saved PR closed or merged
- no other tracked change claims its branch
- no open PR in the same repository uses the saved head ref as its base
- no active member of a GitHub stack still needs the branch

Local descendants do not substitute for the open-PR base check. Descendant visibility is evidence
for selected `sync`, not deletion of a GitHub branch.

Identity and baseline retire only after artifact cleanup succeeds. A tracking record or PR that
cannot be inspected is skipped. Once mutation starts, a failure stops cleanup and leaves later
records for a rerun.

### Adoption and repair

`checkout` adopts review state already on GitHub and never rewrites commits, restacks descendants,
moves the workspace, or mutates PRs. Before `--fetch` imports anything, it reads the PR head's
change ID from the remote object without creating a ref. If a visible local revision already has
that change ID at another commit, it stops and names `relink`; importing would create a divergent
copy that rerunning could not remove. Temporary refs and bookmarks are removed before return.

`relink` explicitly replaces uncertain tracking for one change. It verifies the known PR and
same-repository head branch, then saves the identity and exact observed remote target as one pair.
Replacing the stale baseline lets a later `submit` update the known review rather than reject the
branch as foreign.

`doctor` observes setup and local leftovers from interrupted `checkout --fetch` or `sync`. It
changes nothing without `--fix`; currently the only automatic repair is restoring the reserved
review-branch exclusion in remote fetch configuration. It never mutates GitHub.

### Inspection

`view` and `list` are read-only. Both observe saved review branches directly on the remote and ask
GitHub for current PR state, without fetching.

Neither command guesses. A change with no saved review identity is reported as not submitted,
even if a PR happens to use the branch name that change would generate.

Inspection tolerates history exposed by fetch rather than immediately declaring the stack broken.
`view` walks past immutable or divergent side copies of merged changes. A merged PR still in the
local stack becomes a `cleanup needed` row naming the selected `sync`. Only when no supported
linear walk remains does `view` stop with a targeted diagnostic.

A per-change lookup failure marks only that row unresolved and produces an incomplete report. A
failure before any rows can be built returns its own exit code. `list` includes orphaned PRs as
separate rows.

`view` and `submit` render stack rows through the user's native `jj log` formatting. `--json`
follows [`docs/json-output.schema.json`](../json-output.schema.json) and exposes no cache state,
raw remote targets, or tracking records.

## Rewrite behavior

Most rewrites follow directly from stable change IDs and DAG-derived topology. Two cases need
explicit rules:

- **Abandon**: the change leaves every current local stack and descendants attach to its parent.
  Its PR becomes orphaned. Surviving stacks never close, reuse, or retarget it. Explicit closure
  uses `unstack --cleanup --pull-request <pr>`.
- **Split**: new logical changes get new change IDs and normally new PRs. The change retaining the
  original change ID retains its PR.

### Cross-stack rewrites

When a rewrite moves changes between local stacks, identity still follows full `change_id`, each
stack command still acts on one selected chain, and ambiguous linkage still fails closed. Other
affected stacks wait for their own explicit commands.

- **Move changes between stacks**: submitting one resulting stack updates that chain. Moved
  changes retain their PRs and recalculate bases from their new parents.
- **Split one stack into several**: resulting paths may share reviewed ancestors. If one old
  GitHub stack spans active reviews on more than one desired path, the user dissolves it with the
  named `gh stack unstack <number>` command and submits each path separately.
- **Merge several stacks into one**: submitting the resulting chain reuses reviews by change ID,
  recalculates every base, and produces one overview comment on the new head.

Stacks not yet resubmitted may still show old navigation or overview comments. That is expected:
`submit` does not mutate stacks outside its selection. `view` and `list` identify stale reviews
by comparing each baseline with the current change and name the stack to refresh. Orphaned PRs
need explicit `unstack --cleanup --pull-request <pr>`.

## CLI contract

Built-in `--help` and the user guide own exact parser syntax and aliases. This specification owns
enduring selection rules, command effects, and exit meanings.

Running the executable without a subcommand is equivalent to `view` without arguments.

### Exit codes

Process exit codes are part of the CLI contract. Where a meaning overlaps with the `gh stack`
extension, the code matches. Codes 7-9 remain reserved because their `gh stack` meanings have no
`jj-stack` equivalent.

- `0` — success
- `1` — any other failure, including a lifecycle command blocked before completion
- `2` — the selection does not form a supported local review stack
- `3` — unresolved conflicts block the requested operation
- `4` — GitHub authentication, network, or API failure
- `5` — invalid command-line arguments
- `6` — a selector matched more than one target
- `10` — `view` or `list` printed an incomplete report
- `130` — interrupted

The user-facing table lives in [docs/exit-codes.md](../exit-codes.md).

## Current scope

Supported:

- one review remote and one `github.com` repository per invocation
- linear local review stacks
- visible mutable review changes
- one PR per review change
- operation with or without GitHub stack support

Unsupported:

- stacked reviews crossing repositories or remotes
- every non-`github.com` host
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
