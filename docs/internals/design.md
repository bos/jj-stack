# jj-native stacked GitHub review: design

Status: canonical product specification. This document is the sole behavioral authority for
`jj-stack`. Implementation structure belongs in `implementation-strategy.md`; evidence policy
belongs in the testing and review documents; deferred questions belong in `backlog.md`.

## Summary

`jj-stack` turns a linear chain of `jj` changes into a stack of GitHub pull requests
without making side metadata the source of truth.

The model is small:

- one reviewable unit is one visible mutable `jj` change, identified by its full `change_id`
- one stack is a linear chain of those changes from a chosen head back to `trunk()`
- each tracked change gets one stable remote branch, used as that change's PR head
- the local stack is rediscovered from the `jj` DAG on every run, not from a saved parent map

The only per-change state `jj-stack` saves locally is the PR and branch attached to each change
and the exact commit last sent for review. It also caches one boolean per local/GitHub repository
pair for native stack support. Everything else is observed or derived. That keeps the tool feeling
like an extension of `jj` rather than a parallel stack manager.

## Design goals

1. Make stacked GitHub PRs feel native in a `jj` workflow.
2. Be easy to use.
3. Avoid out-of-band metadata as a source of truth.
4. Keep branch names stable across rewrite-heavy review.
5. Recompute as much as possible from `jj` state on every run.
6. Keep any persisted state optional, minimal, and tool-owned.

## Safety rules, in priority order

These rules are ordered. A lower rule never justifies weakening a higher one.

1. Never lose local work, abandon it silently, or publish it when the user did not ask.
2. Never change a branch, pull request, or repository without first confirming it is the one you
   think it is, and without a guard that fails the write if it moved underneath you.
3. When linkage is ambiguous, stop. Never guess which pull request belongs to a change, and never
   silently adopt one that has appeared in place of another.
4. Merge only the exact commit that was reviewed, and re-confirm the pull request and its head
   immediately before every irreversible step.
5. A command touches only the stack it was asked for. Reaching further has to be asked for
   explicitly.
6. Never drop a saved link to a pull request until doing so is safe — either the user named it
   for removal, or GitHub has confirmed the work landed and no other local stack still depends on
   it. Cleanup left undone must never block unrelated work.

This can't be followed often enough to be a genuine rule, but it's a good UX aspiration:

7. Most stops and warnings try to name a command the user can run next to resolve the problem or
   make progress. They do so when (a) it is clear what the right next thing to do is and (b)
   there's a reasonable probability the condition in question could be seen in the wild.

Three things can answer a question about the state of a review, and each is authoritative for a
different set of questions:

1. The **`jj` DAG** — what changes exist locally, how they are related, and what they contain.
2. **GitHub** — everything about a pull request: whether it exists, its state, its reviews,
   which native stack it belongs to, and what a merge produced. Whether reviewed work actually
   landed is settled only by ancestry from the fetched trunk commit, under [Landed
   evidence](#landed-evidence).
3. **Local tracking** — which pull request and branch belong to a change, and the exact commit
   last sent for review. Nothing else. It exists to stop a command acting on the wrong review,
   and can never authorize one on its own.

Local tracking holds one further fact: whether a given local repository and GitHub repository
support native GitHub stacks. It caches no permissions, no stack shape, and nothing about an
operation in progress.

## Recommended GitHub policy

The repository should protect trunk and allow the merge methods its maintainers intend to use.
Review branches are transport branches, not alternate integration branches. Users should ask
`jj-stack merge` to merge a reviewed path rather than merge an intermediate stacked PR directly.

FIXME: the above isn't really true. We must thrive if github does server-side automerge behind
our backs, or a user initiates a merge through the github UI (I believe these are
indistinguishable).

`jj-stack` does not duplicate repository policy. It does not preflight approvals, checks,
conflicts, merge queues, or auto-merge state across the repository. GitHub applies those rules to
the requested native stack or ordinary PR mutation, and `jj-stack` reports the result.

## Relevant `jj` and Git constraints

A few `jj` and Git properties drive this design:

- GitHub review is still branch-based. Even in a `jj` workflow, GitHub wants a head branch
  and a base branch per PR.
- Review branches need not remain in the local `jj` view. The backing Git store can observe and
  mutate exact remote refs while local topology remains entirely in the DAG.
- Ordinary jj bookmarks still behave normally. `jj-stack` reserves `review/*` for its remote-only
  transport branches and rejects locally imported names in that namespace.
- `change_id` is the durable logical identity of a change across rewrites. The Git commit ID
  is not.
- Notably, `change_id` seems to survive a GitHub rebase merge, which is a very nice property for
  users. It appears to be obliterated by a squash merge.
- `jj`'s internal storage is not an extension API; the tool does not write into `.jj/`
  internals.

## Mental model

### Review change

A review change is one visible mutable `jj` change, identified by full `change_id`. The commit
ID, remote branch name, and current diff base are *not* involved.

"Visible mutable" follows `jj`'s own revsets:

- visible: the commit is in `visible()` (not a hidden predecessor)
- mutable: the commit is in `mutable()`, with immutability defined by the repo's
  `immutable_heads()`

By default that means `trunk()`, tags, and untracked remote bookmarks define immutable
history. If the repo customizes `immutable_heads()`, `jj-stack` honors that rather than
maintaining its own competing notion of what is safe to review or rewrite.

### Review stack

A stack is the linear first-parent chain from a chosen head back to the nearest commit also
reachable from `trunk()`. That commit is the stack's base and is not itself part of the stack.

Commands validate only that one chain. Other visible children elsewhere in the DAG are separate
stacks, not an error: if an ancestor on the chain has other reviewable children, those are
separate PR chains, out of scope unless the command explicitly asks about more than one stack.

`jj-stack` supports only linear stacks: merge commits inside the chain and divergent changes are
rejected. Unresolved conflicts are not a shape problem — `view` and `list` report them — but
`submit` and `sync` refuse to act on a conflicted change. An immutable boundary reached as a
non-first parent of a merge is exposed so recovery can diagnose the landed ancestry, but never
published.

`jj` *can* model all those shapes, but the UX complexity and mental gymnastics that would be
required make them not worth supporting in `jj-stack`.

### Pull request branch

Each tracked review change gets exactly one remote Git branch, used as the GitHub PR head. The
branch name is readable to humans and stable for tooling. `jj-stack` reserves the fixed `review/`
namespace; branches outside the complete managed grammar cannot be adopted. Managed review
branches do not persist as local jj bookmarks.

The initial name is built from:

- the fixed `review/` prefix
- a lowercase ASCII slug from the first line of the commit description
- an eight-character `change_id` suffix

```text
review/<slug-from-subject>-<change_id.short(8)>
```

Example:

```text
review/add-cache-index-ypvmkkuo
```

The slug helps reviewers using the GitHub UI or plain Git. The `change_id` suffix keeps
the name tied to the logical change without becoming noisy. Eight characters is fixed,
readable, and effectively unique once combined with the slug. If two changes in a selection
resolve to the same branch, `submit` stops before mutating anything; a never-submitted change can
use a different subject to produce a different initial slug.

The slug is only an input to the *initial* default name. Once a review is created, its branch is
not automatically renamed when the commit subject changes — title churn must not cause branch
churn during review.

Generate once, then pin: for a change that is already tracked, the saved head ref is the only
branch-name authority, and no ordinary command renames or replaces it from what it discovers.

A `review/` bookmark in the local jj view would make its target immutable, taking the change out
of review entirely. Ordinary fetches therefore exclude the whole `review/` namespace. Two things
can defeat that, and if detected, both will cause commands to stop (printing advice on how to
fix): a jj config override that re-enables fetching those bookmarks, and a `review/` bookmark
that has already been imported.

### Review base

The GitHub base branch for a review change is:

- the parent review change's remote branch, if the parent is also being reviewed
- otherwise the trunk branch

This is where GitHub still imposes a branch model on top of `jj`. `trunk()` defines the
stack boundary in commit space, but it does not by itself give GitHub a base-branch name.
For GitHub operations the tool has to resolve trunk to one concrete remote bookmark on
the chosen remote, e.g. `main@origin`.

The trunk base must be one of:

- the chosen remote's default branch as reported by GitHub
- or an unambiguous remote bookmark on that remote whose target is `trunk()`

If `trunk()` falls back to `root()` or cannot be mapped to exactly one remote bookmark on
the target remote, `submit` errors out rather than guessing.

### Workspaces

Several `jj` workspaces can share one repository, and each has its own working-copy commit.
`jj-stack` never counts one of those as a review change: `list` skips them all, whichever
workspace it was run from. You can still submit your own working copy by naming it explicitly,
as long as it has something in it — an empty working-copy commit is never reviewable.

If `jj` reports that a workspace is stale, the command stops and tells the user to run
`jj workspace update-stale`.

## What is derived vs. stored

### Derived from `jj` every time

These need no tool-owned state:

- stack topology
- parent-child relationships
- diff base inside the stack
- current head commit for a change
- whether a remote review branch needs to move after a rewrite

All of that already lives in the commit DAG, the change-ID model, tracking identity, and direct
remote observation.

### Stored in the tracking-state file

Tracking contains two distinct versioned records keyed by full `change_id`:

- `ReviewIdentity`: repository owner/name, PR number, and one canonical head owner/ref
- `SubmittedBaseline`: the last successfully submitted `commit_id` for that identity

(We do not support GitHub enterprise appliances, only `github.com`.)

Two checks against those records recur throughout this document. They are defined here and cited
by name everywhere else:

- **identity match** — the live PR's repository, PR number, and head owner/ref all equal the
  saved `ReviewIdentity` fields.
- **snapshot match** — an identity match whose live PR head SHA also equals
  `SubmittedBaseline.commit_id`.

Neither check authorizes a mutation on its own. Safety rule 4 requires the mutating command to
re-read and recheck immediately before each irreversible action.

[Identity and mutation safety](#identity-and-mutation-safety) says which commands may change a
saved identity, and which may advance a baseline.

Nothing else about a review is written down. Whether the pull request is open, whether it is a
draft, who has reviewed it, whether it merged, where it sits in the stack, whether it is safe to
clean up — all of that is asked for when it is needed and thrown away afterwards.

Tracking also remembers whether GitHub offers native stacks in that repo. The first command that
needs to know asks GitHub and persists the answer.

A change can be in one of two tracking states:

- **untracked**: no record yet. Predicted branch names and remote observations alone do
  not count as tracking.
- **tracked**: a `ReviewIdentity` record exists; the tool inspects the exact saved PR and
  branch. A complete submitted review also has a `SubmittedBaseline`.

PR rediscovery is an explicit recovery flow: ordinary commands never replace a missing, closed,
moved, or ambiguous review automatically. They preserve the saved identity and name `relink`, or
`unstack --cleanup` followed by a fresh `submit`.

Recording a new submitted commit cannot overwrite PR identity: every write goes through a
compare-and-swap that fails if the identity changed underneath it. Reads isolate individual
absent, malformed, or obsolete records: one bad record is reported on its own, telling the user to
repair it with `relink`, and the other records stay usable. An unreadable or unsupported top-level
file blocks every command that loads tracking state, reports its exact path, and tells the user
how to move it aside before re-adopting reviews through `checkout` or `relink`. There is no
migration or automatic discard path.

User-authored settings live in `jj` config under `[jj-stack]`, not in the tracking-state file:

```toml
[jj-stack]
reviewers = ["octocat"]
team_reviewers = ["platform"]
labels = ["needs-review"]
```

`submit --reviewers`, `--team-reviewers`, and `--label` override these for one invocation. A key
that looks like a typo of a known one is rejected with a suggestion; an unrelated key is
ignored.

Managed comments are derived output, not a source of truth. In a repository without native GitHub
stack support, `submit` regenerates navigation comments from the current `jj` stack. In every
repository, explicit or helper-generated stack prose is stored in one overview comment on the
selected head PR. `submit`, `unstack`, and `cleanup` may read comments to re-find or delete
comments the tool previously wrote, but `view` does not inspect issue comments.

## Storage strategy

Do not write into `jj` internals (`.jj/repo/store/extra/`, the view/op store, private
ref namespaces). Those are tempting but tie the tool to storage details `jj` keeps
flexible.

Do not store config or tracking state in the working tree. Tracked workspace files are
the wrong default for both:

- config in the working tree looks like project-shared policy and is too easy to commit
- tracking state in the working tree dirties the `jj` working copy and perturbs the
  history the tool is supposed to map to GitHub

So storage splits in two:

- human-authored config in `jj`'s normal config scopes under the `jj-stack` namespace
- tracking state in `~/.local/state/jj-stack/repos/<repo-id>/state.json`

Repo defaults follow `jj`'s own precedence:

- user config (`jj config edit --user`)
- repo config (`jj config edit --repo`)
- workspace config (`jj config edit --workspace`)

That keeps `jj-stack` aligned with `jj`'s config model rather than inventing a parallel
conditional-matching system.

State is repo-scoped, so every workspace for the same repo shares one location without a separate
bootstrap step and without writing any tool-specific file into the workspace.

Mutating commands serialize against each other through a repo-scoped advisory lock. Read-only
commands do not take it and never write tracking observations.

The lock only serializes concurrent processes. Commands do not persist their planned selection,
selected parent chain, progress phase, or remaining work. After an interruption, the next command
rereads `jj`, the remote, and GitHub and computes what remains.

## Policies

These are the rules commands obey. Each is the authority for its rule: where an earlier section
introduces the same idea, it points here rather than restating it.

### Selection

Lifecycle commands act on one stack, headed by `@-` when no `<revset>` is given. `@` is always
explicit user intent and is never chosen by an omitted argument. `relink` has no default at all —
both its selectors are explicit.

Three modes act beyond one stack, and each must be asked for explicitly: `sync --all`, which
cannot be combined with a selector; `cleanup`, which sweeps every tracked record; and
`unstack --cleanup --pull-request orphans`, which acts on every tracked pull request whose local
change is gone. No default invocation of any command reaches beyond the selected stack.

What a stack is, and which shapes are rejected, is defined under [Review stack](#review-stack).

Ambiguity always fails closed.

### Identity and mutation safety

**identity match** and **snapshot match** are defined under
[Stored in the tracking-state file](#stored-in-the-tracking-state-file).

Before mutating anything, a command establishes the identity its mutation depends on for every
change in the selection, so a mid-stack failure cannot leave siblings half-applied:

- `submit` requires an identity match per tracked change, and requires each remote review ref to
  hold either its submitted baseline or the current local commit left by an interrupted push —
  that push counts as complete only on an identity match whose live PR head SHA equals the current
  local commit.
- `merge` requires more: current local commit and remote review ref both equal
  `SubmittedBaseline.commit_id`, and the live PR is a snapshot match. Diff or tree equivalence is
  never sufficient.
- `sync --all` and `cleanup` require a snapshot match before retargeting, closing, or retiring.

Safety rule 4 then requires re-reading those facts immediately before each irreversible action; a
planning observation never authorizes a later mutation. A remote swap, repository retarget,
renamed head, moved branch, missing PR, or replacement PR fails closed, naming `relink` or
`unstack --cleanup` followed by a fresh `submit`.

Observation never rewrites identity. Only review creation, `relink`, `checkout`,
`unstack --local`, and cleanup that has passed its eligibility checks may change one.

A `SubmittedBaseline` records the exact commit last sent for review, so only the commands that
send or adopt one may advance it: `submit` after a successful push, `relink` from the observed
remote target, `checkout` when it adopts an existing review, and selected `sync` when it adopts
the commits GitHub produced by rewriting reviews that did not merge. `merge`, `sync --all`,
`cleanup`, `view`, and `list` never do.

### Landed evidence

Two observations prove that reviewed work landed:

- **exact submitted commit on trunk** — the baseline is an ancestor of fetched trunk and the live
  PR is a snapshot match. A pull request in a GitHub stack must additionally report merged
  before `sync` may act on it.
- **selected PR's rewritten merge result on trunk** — the saved PR reports merged, its live
  merge-result commit is an ancestor of fetched trunk, and its head remains the submitted commit.
  This covers squash and rebase results.

Selected `sync` may act on either. `sync --all` may act only on the first, because the second is
selected-stack evidence and does not authorize repository-wide change. A PR merely reporting
merged, or a merge result no longer reachable from fetched trunk, authorizes nothing: preserve
local revisions, identity, and baseline, and name the `sync` to run once trunk is restored.

`sync` removes the tracking for a landed change only once it has successfully updated the
changes that did not land, and only if no other visible stack still needs that tracking. If one
does, the tracking stays and `sync` names each dependent stack and the exact command that would
finish it.

GitHub preserves jj's `change-id` header through native and ordinary rebase merges but not squash
merges. A matching change ID identifies the landed successor; otherwise the old local change is
retired without relabeling the landed commit or storing an alias.

### Native stack membership

GitHub's own support for stacked pull requests is in limited alpha, so most repositories do not
have it and `jj-stack` has to work either way. Where it is available, a **GitHub stack** is
GitHub's own object: an ordered list of pull requests it manages as a group. Where it is not,
`jj-stack` supplies the missing navigation itself, by writing comments that link the pull requests
together — if GitHub ships the feature broadly, that whole comment mechanism can go. Once a pull
request in one has merged, GitHub keeps it in the list as history and it can no longer be moved.
The unmerged ones above it are the only ones that can still be reordered, removed, or retargeted,
and this document calls those **open members**. Whether a repository supports GitHub stacks at all
is the cached boolean described under [Stored in the tracking-state
file](#stored-in-the-tracking-state-file).

**Membership rule.** Every open member of a GitHub stack the selection touches must belong to the
selected local parent chain, and a selection may touch only one GitHub stack it could still have
to change. The rule looks only for open members the selection leaves out, so a review GitHub no
longer lists never blocks, and a GitHub stack holding nothing but merged pull requests is not in
the way. An open member the user did not select, or membership that changed while the command was
running, fails before any mutation, and tells the user to dissolve the GitHub stack with `gh stack
unstack`. Five jj-stack commands apply this one rule and re-check it immediately before the
mutation it authorizes: `submit`, `merge`, selected `sync`, `unstack`, and `cleanup`.

A GitHub merge may rewrite the open members above the ones it merged. Selected `sync` then adopts
the exact commits GitHub produced rather than replaying the same diffs, and accepts the new heads
and bases only while a merged pull request it still tracks confirms the transition — GitHub
reporting what it just did, not an inference from matching trees.

### Branch transport

Review branches move in one atomic push, and every ref in it says what jj-stack expects the
remote to be holding right now. If anything moved in between, the whole push fails and none of it
lands; there is no falling back to pushing refs one at a time. jj-stack will not take over a
branch it has no record of. A topology change counts as a change even when the diff does not.

A PR's base is its parent's review branch, or trunk for the bottom change — chosen by position
in the stack, not by whether the parent's PR is still open. If a parent's PR is not open, `submit`
stops before mutating anything rather than reaching past it.

Intermediate GitHub states are allowed but must preserve review identity: a rewritten stack must
never leave a selected PR based on a branch that now contains its head, which GitHub reads as
merged. An interrupt between the protective retarget and the final PR sync should leave a
repairable flat or partly restacked set of the same open PRs, never closed or replaced reviews.

### Derived artifacts

Titles, bodies, navigation comments, and the stack overview comment are re-derived on every submit
and are never a topology source; see [description helpers](../description-helpers.md). Managed
comments are rediscovered by an unambiguous body marker, never by a stored comment ID, and
ambiguity leaves the comment alone for later. Navigation comments exist only where GitHub has no
native stacks. Draft state is never lost by accident, and plain `submit --draft` never
un-publishes a PR.

### Cleanup eligibility

Cleanup removes only derived artifacts named by one complete identity and baseline pair: managed
comments, remote review refs under an exact expected-target lease, and the records themselves.

A record is eligible only when GitHub reports the exact saved PR closed or merged, no other
tracked change claims its branch, and no open PR in the same repository — tracked, untracked, or
orphaned — uses the saved head ref as its base. Local jj descendants do not substitute for that
last check: descendant visibility authorizes selected `sync`, not deleting a GitHub branch. An
open member of a GitHub stack keeps its branch, while one GitHub retains only as merged
history does not block otherwise authorized cleanup.

The identity/baseline pair is retired only after all authorized artifact cleanup succeeds. Damaged
or individually failing records are reported and skipped without blocking independent work, failed
cleanup leaves safe leftovers, and every warning names the command that finishes the job.

### Inspection

`view` and `list` change no review state — no pull request, no branch, no tracking record.
`--fetch` is not inert, though: it rewrites the remote's fetch refspec to exclude `review/*` if
that is not already in place, and runs an ordinary `jj git fetch`, which snapshots the working
copy. Neither command guesses: a change never attached to a review is reported as not submitted,
rather than resolved by looking for a pull request on the branch name that change would have
produced.

They tolerate the history a fetch exposes rather than calling a stack broken: `view` walks past
immutable or divergent side copies of merged changes, and a merged PR still on the stack becomes a
`cleanup needed` row naming the exact selected `sync`. Only when no supported linear walk remains
does `view` stop, with a targeted diagnostic rather than a traceback.

When they cannot resolve something they say so in the row it belongs to and exit with the
incomplete-report code. A run that cannot produce a report at all exits with whatever code
describes why instead. `list` surfaces orphaned PRs as
their own rows; without that, squashing two reviewed changes silently leaves a PR open.

`view` and `submit` render stack rows through the user's native `jj log` formatting. `--json`
output follows [`docs/json-output.schema.json`](../json-output.schema.json) and exposes no cache
state, raw remote targets, or saved tracking records.

## Commands

What each is for and what it may change. The policies above constrain all of them.

- **`submit`** — publishes the selected stack, and is the only command that creates a PR or
  publishes a never-submitted change. After everything succeeds it refetches every PR that was
  open at the start and fails loudly, naming them, if any is now closed or missing: detection,
  not repair.
- **`view`**, **`list`** — report.
- **`sync`** — reconciles one stack with what landed: rebases the changes that did not land onto
  fetched trunk, updates their existing reviews, and drops the tracking for the ones that did.
  Never creates a PR. Does not rebase merely because trunk advanced — `jj rebase` owns that. Stops
  before rewriting if that would discard unpublished work, if the remainder is nonlinear, or if an
  unreviewed change sits between reviewed ones.
- **`sync --all`** — repository-wide and deliberately weaker. May only retarget and close reviews
  proven landed by the first landed proof; never rewrites stacks, submits work, or creates PRs.
  It continues past damaged records so one bad record cannot block the rest.
- **`merge`** — the only command that asks GitHub to merge. It never pushes trunk and never
  touches local history. Candidates are the contiguous open non-draft PRs from the bottom; the
  first draft or closed-unmerged PR blocks it and everything above. `--pull-request` stops at the
  change linked to that pull request, taking the candidate prefix through it rather than the whole
  path. Pull requests in one GitHub stack merge as a single asynchronous group request; everything
  else merges bottom-up through the ordinary API, stopping at the first rejection with PRs below
  it already merged. Rebase merge is refused for more than one ordinary PR, because the first
  rewrite invalidates the reviewed commit identity of the rest.
- **`unstack`** — ends review: closes the open PRs it already tracks, retains their identities so
  later cleanup can prove what it is acting on, and with `--cleanup` also removes verified
  artifacts. Closing and cleaning up a review is also how a stack is started over: `submit`
  afterwards opens fresh pull requests under the ordinary generated names. `--local` drops local
  tracking only, touching neither GitHub nor local history. Rerunning it is always safe.
- **`cleanup`** — sweeps every tracked record and removes the artifacts of reviews that are
  finished, checking each one against the eligibility rules above. Never a correctness
  prerequisite, never local-history repair.
- **`checkout`** — adopts review state that already exists on GitHub. Sets up tracking only:
  never rewrites commits, restacks descendants, moves the workspace, mutates PRs, or leaves
  import artifacts behind. Before `--fetch` imports anything it reads the PR head's change ID
  from the remote object without creating a ref; if a visible local revision already holds that
  change at another commit it stops and names `relink`, because importing would leave a divergent
  copy no rerun can remove.
- **`relink`** — reattaches one known PR, and its same-repository head branch, to one change, for
  a review whose identity the user knows but the tool cannot prove. It saves the PR identity and
  the exact observed remote target as the baseline together; replacing a stale baseline is the
  point, since that is what lets a later `submit` update the review instead of rejecting the
  branch.
- **`doctor`** — read-only setup and connectivity report, and the observation point for leftovers
  from an interrupted `checkout --fetch` or `sync`. A failed check skips the checks that depend
  on it, so one root cause yields one diagnosis.
- **`completion`** — prints shell completion scripts; inspects nothing.

## Rewrite behavior

This design behaves well under normal `jj` rewrite-heavy workflows:

- **Rebase**: the commit ID changes and the `change_id` stays stable. Re-running `submit` moves
  the saved remote review branch to the rewritten commit and updates the existing PR.
- **Squash or amend**: same as rebase. If the workflow then abandons a now-empty
  change (the usual way to collapse two reviewed changes into one), Abandon rules
  apply to that change.
- **Reorder or reparent**: the stack is rediscovered from the DAG; PR base branches
  are recalculated.
- **Insert**: a new mutable change appears on the chain. `submit` opens a PR for it
  and any descendants' PR bases recalculate against the new parent.
- **Abandon**: the change leaves every current local stack and descendants reattach
  to its parent. Its PR becomes *orphaned* — surviving stacks never close, reuse, or
  retarget it. Cleanup removes its saved identity only after verifying the exact PR is closed or
  merged and its artifacts are safe to remove. An absent PR fails closed and leaves tracking for
  explicit repair or later verification. Explicit closure goes through
  `unstack --cleanup --pull-request <pr>`.
- **Split**: new logical review changes get new change IDs and usually become new
  PRs. The original keeps its `change_id` and PR and is updated normally on next `submit`.
- **Duplicate**: the duplicate has a new `change_id` and is treated as a new
  reviewable change on whatever stack it lands on; the original keeps its PR
  untouched.
- **Ancestor merged on GitHub**: selected `sync` proves the merge result on fetched trunk and
  rewrites local history, which is what removes the merged ancestor from the chain. Until then
  `submit` stops rather than guessing a new base.

### Cross-stack rewrites

When a rewrite changes which stack a change belongs to, the established rules still
hold: identity is by `change_id`, each command operates on one selected stack
(defaulting to `@-`), and ambiguous linkage fails closed. Other affected stacks wait
for their own explicit command.

- **Move changes between stacks**: submitting the user's selected resulting stack
  updates that chain's PRs from the current DAG. Moved changes keep their existing
  PRs and recalculate their bases from the new parent chain.
- **Split one stack into two or more**: the resulting reviewed paths may keep common ancestors.
  When one old native GitHub stack spans more than one desired path, the user explicitly
  dissolves it with `gh stack unstack <number>`, then submits each resulting stack separately.
  Otherwise, submitting one result updates only that chain and every other result waits for its
  own command.
- **Merge two or more stacks into one**: submitting the merged stack updates every
  change on the chain bottom-up, reusing existing PRs by `change_id` and
  recalculating bases. The merged chain ends up with one overview comment on its new
  head and no internal trace of the old stack boundary.

Stacks the user has not yet resubmitted may still display old navigation or overview
comments. That is expected — `submit` does not chase comments on stacks it isn't
operating on, and `merge` does not block on stale state outside the selected stack.
`view` and `list` surface those stacks by comparing each saved baseline against the current
commit, naming their heads and directing the user at `view` for the per-stack next step.
Orphaned PRs left behind by a cross-stack rewrite need an explicit
`unstack --cleanup --pull-request <pr>`.

Identity and baseline left after an interrupted command are safety checks, not instructions to
resume the original selection. A later command acts on the current DAG and live remote state. It
removes those records only after proving the remote result and that no visible stack still needs
the link.

This is exactly the kind of rewrite-heavy flow `jj` is good at.

## Why no parent metadata

A branch-first review tool often has to remember both a named parent and an exact
parent revision because the review boundary is otherwise ambiguous after rewrites.

In `jj`, the boundary is already the commit's parent relation. The only place branch
identity still matters is at the GitHub boundary, because GitHub wants:

- one head branch per PR
- one base branch per PR

So the tool needs remote PR branches, but it does not need persistent local review bookmarks or a
saved parent graph.

## CLI shape

Built-in `--help` and the user guide own command names, aliases, and exact parser syntax. This
specification records only the enduring selection rules and effects of those commands.

Run with no subcommand, the executable behaves the same as `view` with no arguments.
Selection defaults are specified under [Selection](#selection).

Three submit flags carry behavior the parser cannot express, so they are recorded here.
`--reviewers` and `--team-reviewers` request those reviewers even when the pull requests are
otherwise unchanged, and never remove reviewers they omit. `--re-request` likewise acts on an
otherwise-unchanged pull request, asking again only for users whose latest opinionated review —
ignoring plain comments — approved or requested changes; it adds requests and never cancels a
pending one.

### Exit codes

Process exit codes are part of the CLI contract. Where a meaning overlaps with the
`gh stack` CLI extension, the code matches, so scripted callers can treat the two tools
alike; codes 7-9 stay reserved because their `gh stack` meanings (rebase in progress,
lock contention, stacked-PR feature unavailable) have no jj-stack analog.

- `0` — success
- `1` — any other failure, including lifecycle commands that stopped on a blocked action
- `2` — the selection does not form a supported review stack
- `3` — unresolved conflicts in the selected changes block the operation
- `4` — GitHub authentication, network, or API failure
- `5` — invalid command-line arguments
- `6` — a selector matched more than one target and the command failed closed
- `10` — `view` or `list` printed an incomplete report
- `130` — interrupted

`view` and `list` reserve the error codes for runs that cannot produce a report at all; a run that
prints a degraded report exits with the incomplete-report code instead. The user-facing table
lives in [docs/exit-codes.md](../exit-codes.md).

Notable absences:

- no standalone `rebase` command — `jj` already handles descendant rewrites better
  than Git

## GitHub mutation safety

Every GitHub mutation the tool issues is listed here with the destructive default GitHub may take
in response and the policy that prevents it. Any new mutation must be added, and any without a
defense must either prove the destructive default does not apply or gain one before merging.

- **Push of review-branch refs.** GitHub re-evaluates every open PR after a push and auto-closes,
  as merged, any whose head ref is now contained in its base ref — which a reordered stack can
  cause. This is the one defense with machinery of its own: before pushing, `submit` simulates the
  post-push commit IDs of every open PR's head and base, and pre-retargets any at-risk PR to trunk
  so the push never lands with a head contained in its base. The post-push PR sync restores the
  stacked bases. To find what a base ref currently points at, the simulator looks in the push set
  first, then asks the push remote directly. If it can do neither it skips that PR rather than
  guess, and `submit`'s closing open→closed check catches whatever it could not model.
- **Deletion of a remote review branch.** GitHub closes any PR whose head ref points at it.
  Covered by cleanup eligibility, under an exact ref lease; a failed check keeps both branch and
  records.
- **`update_pull_request(base=…)`**, **`create_pull_request`.** A base that already contains the
  head triggers the merged auto-close. Covered by branch transport: bottom-up ordering means a
  parent branch always holds an ancestor of its child's head. If the pull request belongs to a
  GitHub stack, jj-stack re-checks membership and dissolves that whole stack before changing any
  base — GitHub offers no way to remove one member. In an ordinary `merge` it retargets the
  candidate to trunk immediately before asking GitHub to merge it, passing the head commit it
  expects to find.
- **`update_pull_request(title|body)`.** Carries only changed fields and never `base` on a
  content-only refresh, so the previous entry governs every request that does carry one.
- **`close_pull_request`.** Destructive by design. Covered by identity and mutation safety:
  `unstack` acts only on explicit instruction, and either `sync` closes a review only on landed
  evidence revalidated immediately before the call.
- **`merge_pull_request`**, **native asynchronous merge.** Destructive by design; the native form
  merges everything below the target and may rewrite what is above. Covered by identity and
  mutation safety — a snapshot match plus expected state, base, and head SHA, re-read immediately
  before the request. Only a terminal merged result is success. A rejection stops there, with PRs
  below already merged.
- **Creating a GitHub stack, appending to one, and dissolving one.** GitHub puts each admitted
  pull request in exactly one stack, and offers no way to remove a single member. Covered by the
  membership rule, re-checked immediately before the mutation; an incomplete removal stops before
  any branch or base change. GitHub's mutation is the admission authority, and there is no
  fallback to comments after it rejects one.
- **`convert_pull_request_to_draft`**, **`mark_pull_request_ready_for_review`.** Repo policy may
  dismiss approvals on draft conversion or trigger required CI on ready-for-review. Invoked only
  for `--draft=all` and `--open` respectively, never by default. New PRs are created directly as
  draft or published and never round-trip through these.
- **`delete_issue_comment`.** Covered by derived artifacts: rediscovery by body marker, with
  multiple matches rejected.
- **`add_labels`**, **`request_reviewers`**, **`create_issue_comment`**,
  **`update_issue_comment`.** Additive; no destructive default.

## Current scope

Supported:

- one selected review remote per invocation; resolution prefers `origin` or an unambiguous sole
  remote
- one GitHub repo target
- linear stacks
- visible mutable changes
- one PR per reviewable change

Unsupported:

- stacked reviews that cross repos or remotes

Which stack shapes are rejected is specified under [Review stack](#review-stack).

## Bottom line

The central insight is simple:

In a branch-first review tool, stack metadata often becomes part of the core model. In
`jj`, the stack model is already the commit DAG. The tool's job is just to map that DAG
to GitHub's branch-based PR API with stable remote branches.

## References

The design above relies on these upstream `jj` references:

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
