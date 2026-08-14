# Daily workflow

This is the normal author loop `jj-stack` is designed around.

## 1. Build your local stack with `jj`

Create some local changes that you want reviewed. For example:

- refactor the shared model
- add the API
- add the UI

Keep your stack linear (or rewrite it to be linear prior to review). `jj-stack` is
intentionally focused on one linear stack at a time.

## 2. Inspect before submitting

`jj-stack` will by default submit the current stack ending at `@` when the working-copy change
has a description and contents. If the working copy is empty or undescribed, it uses `@-`
instead. In the common case, this is the stack you just built on top of `trunk()`. If `trunk()`
has advanced since you last rebased, your stack instead starts from an older ancestor of
`trunk()`. `jj-stack view` will show the ancestor in the footer beneath your stack, so you can
see exactly what the stack is based on.

You can easily check what the tool thinks that stack is:

```bash
jj-stack
```

This is the same command as `jj-stack view`.

This is a good go-to command whenever you are unsure what your stack looks like or what you have
submitted for review.

If the selected history is not currently eligible for review, `view` still prints the useful
local and GitHub state it can resolve. Empty or undescribed working-copy changes, divergent
changes, conflicts, and merge commits produce warnings instead of blocking inspection. For a
merge, the report follows the first-parent path. Commands that change review state remain strict
and tell you what must be resolved first.

In a large or busy project, you'll often be working on multiple stacks at a time. If you want a
repo-wide inventory of the stacks you have in flight, use the `list` command (or its short alias
`ls`):

```bash
jj-stack list
```

Commands operate on the selected parent chain. If a reviewed ancestor also has another local
child, that child is a separate path; you do not need to rebase it away before working with the
selected stack.

## 3. Submit the stack

Create or refresh the GitHub pull requests for the current stack:

```bash
jj-stack submit
```

If you want to first inspect what `submit` *would* do, without making any changes:

```bash
jj-stack submit --dry-run
```

Each PR's title is the change's subject line, and its body is the rest of the change
description. When a description has no body, `submit` uses your repository's pull request
template (for example `.github/PULL_REQUEST_TEMPLATE.md`) if one exists.

To review the titles, bodies, and draft state of each PR in your editor before anything is
pushed:

```bash
jj-stack submit --edit
```

Each change section contains `JJ: Draft: yes` or `JJ: Draft: no`. Edit that value to choose which
parts of a stack are ready for review; `y` and `n` work too. Saving the document continues the
submit. An invalid value or a non-zero editor exit aborts with nothing pushed.

If you already have a PR body in a Markdown file, attach it while submitting:

```bash
jj-stack submit --describe <change-id>=pr-body.md
```

For a multi-change stack, `--describe stack=stack-overview.md` adds text to the head PR's stack
overview comment.

If a change does not already have its review branch and PR set up, `jj-stack submit` creates the
remote branch and PR. After that, it reuses the saved human-readable branch name as the stable
GitHub PR head while you revise your local change.

To open the stack for early feedback without inviting merges, submit it as drafts:

```bash
jj-stack submit --draft
```

That creates new PRs as drafts and leaves existing ones alone. `--draft=all` also returns already
open PRs to draft, and `--open` marks existing drafts ready for review:

```bash
jj-stack submit --open
```

These flags provide the initial values when combined with `--edit`, where you can override the
draft state for each PR.

This matters for merging: `jj-stack merge` skips a draft PR and everything above it, so a draft
left at the bottom of the stack blocks the whole merge.

### Start a separate review from a reviewed change

To keep a child path in its own GitHub review, name the reviewed parent explicitly:

```bash
jj-stack submit --base <parent-change-id> <child-head-change-id>
```

Only the changes after the parent through the child head are submitted. The parent PR is not
changed. A one-change child is an ordinary PR; a child with two or more changes is a separate
GitHub stack. This also supports two sibling reviews based on the same parent: run one bounded
submit for each child head.

The boundary is not saved. Repeat `--base <parent-change-id>` every time you refresh that child.
Without it, ordinary `submit` follows the whole path to `trunk()` and may include or regroup the
parent review.

Do not merge the child while it is based on the parent review. Merge the parent first. After a
direct merge, `jj-stack merge` tries to sync it automatically; after a queued or external merge,
run `jj-stack sync <parent-head-change-id>` when GitHub finishes.

The exact change passed to `--base` controls this transition. Keep repeating that base while its
PR is open. Once it lands, move exactly the child range onto trunk and refresh it as an ordinary
review, even if a higher change in the parent review remains open:

```bash
jj rebase -r '<child-bottom-change-id>::<child-head-change-id>' -o 'trunk()'
jj-stack submit <child-head-change-id>
```

The bounded revset leaves sibling paths alone. The ordinary submit changes the bottom child PR's
base to trunk; the child can then be merged normally.

## 4. Revise locally as reviews come in

During review, you can make any changes you want with `jj`. Split, squash, reorder, or rewrite
locally as needed.

Once the local stack looks right again, refresh GitHub:

```bash
jj-stack submit
```

`submit` automatically refreshes GitHub's grouping after you delete, reorder, split, or combine
changes. If only one PR remains, it removes the grouping and leaves that PR open.

At a reviewed fork, leave the fork in its parent review and submit each outgoing child with
`--base <fork-change-id> <child-head-change-id>`.

When moving a change between two submitted stacks, submit the source stack first and the
destination stack second. The first command releases the moved PR from its old grouping; the
second joins it to the destination. A destination-first attempt stops before changing anything.

If you want to ask prior reviewers to take another look after you've addressed feedback, run:

```bash
jj-stack submit --re-request
```

This will notify reviewers who approved or asked for changes to a PR.

## 5. Check readiness

Use `view` when you need to answer:

- which changes already have PRs
- which PRs are draft, approved, have changes requested, or need cleanup

If review state already exists on another machine or only on GitHub, connect it to local changes
with:

```bash
jj-stack checkout --pull-request <pr>
```

`checkout` fetches the reviewed commits through the selected PR, saves local tracking, and runs
`jj edit` on that PR's change. Its temporary import bookmark is removed. A custom fetch
configuration may also expose ordinary review bookmarks; those do not prevent adoption when they
match the saved review. To create a new change on top instead of editing the selected change, run
`jj new` afterward.

`--pick` is the interactive form for a stack this repository already tracks. It lists each
tracked stack's head change ID and subject, asks for a number, and edits that head. It does not
discover GitHub-only stacks; use `--pull-request <pr>` when continuing work from another machine.

If you want to inspect the stack for one linked PR directly:

```bash
jj-stack view --pull-request 7
```

(You can use `-p` as an alias for `--pull-request`.)

A change ID, including a short prefix, and a linked PR show the complete local stack containing
that change. An arbitrary revset instead selects the exact revision at which the displayed stack
ends. After you fetch an ordinary GitHub rebase merge, a change ID or PR selects the remaining
local revision instead of the landed commit. If two local revisions or several containing paths
are visible, `jj-stack` stops; use `jj log -r 'change_id(<change-id>)'` to inspect them and choose
or reconcile the path you want.

If you want to inspect several stacks in one run, pass several selectors in
the order you want them shown:

```bash
jj-stack view foo --pull-request 7 bar
```

For more detail, pass `--verbose`:

```bash
jj-stack view --verbose
```

## 6. Ask GitHub to merge reviewed changes

When the bottom part of the stack is ready:

```bash
jj-stack merge
```

`merge` considers the consecutive open, non-draft PRs from the bottom of the stack. It does not
try to duplicate GitHub's rules for approvals, checks, conflicts, or repository policy. It only
checks whether the trunk branch uses a merge queue so it can send the request by the right route.
GitHub evaluates the remaining rules.

If you rewrote a reviewed change, rerun `submit` before merging even when the diff is unchanged.
`merge` accepts only the exact commit last sent for review when the review branch and PR still
point to it. It will not refresh a review to make the change mergeable.

Your stack does not have to be rebased onto the latest `trunk()` first. Trunk moves under you all
the time, and GitHub merges a PR whose base is behind as long as it does not conflict.

If it does conflict, GitHub refuses and `merge` says so, naming the way out:

```text
Merge blocked:
  ✗ stop: at PR #7 for add the API qpvuntsm: GitHub will not merge it: Pull Request is not
    mergeable; if it conflicts with main, rebase onto trunk(), resolve the conflict, and run
    jj-stack submit qpvuntsm before merging again; if a check or repository rule is failing, fix
    that on GitHub first
```

Rerunning `merge` will not clear a conflict — the reviewed commit has to change:

```bash
jj rebase -r '<bottom-change-id>::<head-change-id>' -o 'trunk()'
# resolve conflicts with your normal jj workflow
jj-stack submit <head-change-id>
jj-stack merge <head-change-id>
```

If GitHub refuses a stack merge, nothing merges.

If a lower PR of your own already merged elsewhere, `merge` stops at it and names
`jj-stack sync <head-change-id>` instead — your stack still holds a local copy of work that is
already on trunk.

To preview the same selection and validation without asking GitHub to merge:

```bash
jj-stack merge --dry-run
```

To stop the selected bottom portion at one pull request:

```bash
jj-stack merge --pull-request 7
```

That pull request sets only the merge boundary. After a direct merge, `merge` still updates the
whole local stack containing it, including higher changes whose review branches GitHub rewrote.

For a direct merge, the merge method comes from repository settings when only one is enabled.
When several are, set a default once:

```bash
jj config set --repo jj-stack.merge_method squash
```

Or choose one per run, which overrides that default:

```bash
jj-stack merge --method squash
```

GitHub reports which methods a repository allows but never which one to prefer, which is why one
of these is needed. A merge queue chooses the method itself. If you pass `--method` for a queued
review, `jj-stack` warns and ignores it.

GitHub handles both one-PR and multi-PR selections through its asynchronous merge API. A failed
operation merges nothing. GitHub may rewrite the branches for PRs that remain above a partial
selection.

When trunk uses a merge queue, `merge` returns successfully once GitHub reports that the selected
PRs are in the queue:

```text
In merge queue:
  ✓ GitHub merge request: PRs #41, #42 are queued for main through commit abc123
GitHub will merge them once the queue processes them.
```

That result does not mean trunk changed. `view` and `list` show the PRs as queued. Wait for GitHub
to merge them before running `sync`; there is no queue watcher in `jj-stack`.

When a direct merge completes, `merge` fetches the result, removes the merged local changes,
rebases any selected survivors, and updates their existing PRs before returning. If that local
update stops, the command says that GitHub already completed the merge; do not run `merge` again.
It continues with the containing-stack head that it resolved before merging, even if the original
selector was a revision expression whose meaning changed with trunk. Follow its recovery
instructions, which may include rerunning `sync` with that head change ID.

If an identical stack request is already pending, wait and rerun the same explicit `merge`
command. Once GitHub finishes, the retry observes the completed result and updates the local
stack.

While an open PR is queued, `submit` makes no changes to its selected stack. New local changes
above the queued PR remain unsubmitted. Wait for the queued changes to merge, run `sync` with the
new head change ID, then run `submit` with that same change ID. `sync` also leaves a queued stack
alone. Independent stacks remain usable.

## 7. Apply completed GitHub merges locally

Use `sync` to apply completed GitHub merges to the selected local stack and refresh the PRs that
remain. Run it after a queued or external merge, or when automatic sync stopped and your local
stack still contains old merged commits:

```bash
jj-stack sync <head-change-id>
```

`sync` fetches trunk, verifies which lower PRs GitHub merged, rebases the remaining selected
changes, and updates only PRs that already exist for them. It does not open a PR for trailing WIP
or update reviews outside the selected stack. After that succeeds, the same command removes
merged review branches, overview comments, and tracking that are no longer needed. If you have
more local changes built on top of the selected stack, `jj` rebases those changes too so they
remain on top. Its output describes those sync and cleanup actions, even when no reviews remain.

GitHub may preserve a change as it merges or create a different commit, as a squash merge does.
`sync` handles either result without pretending the new GitHub commit is the old local change.
If a stack merge also rewrote the PRs that remain open, `sync` adopts those exact reviewed
commits and rebases only your trailing local work above them.

The local DAG stops `sync` before it rebases in these cases:

- A remaining change has multiple visible revisions, so there is no single revision to rebase.
- A merged change has local edits made after submit. Removing it would discard those edits.
- A local change that has not merged is a parent of reviewed work that has merged. Moving the
  local change could put it before or after the merged work, and `sync` will not choose for you.
- An unreviewed change sits between reviewed changes. `sync` updates existing PRs but never
  creates the missing review.

When the order between an unmerged parent and merged review is ambiguous, the diagnostic prints
the relevant change IDs and commits plus an exact `jj log` command. Inspect both histories and
choose the order with ordinary `jj`; ask an agent to inspect the repository if useful. Then run
`jj-stack view`. Sync a remaining mutable reviewed head, or run `jj-stack cleanup` if no reviewed
local copy remains.

Before rebasing, `sync` also checks the configured remote, fetched trunk, saved PR links, and
GitHub stack membership. A missing, moved, closed, or ambiguous review stops the run before local
history changes. The diagnostic names the state to inspect or repair.

Conflicts do not prevent the local rebase. If a reviewed change remains conflicted afterward,
`sync` leaves the rebase in place and stops before updating that PR. Resolve the conflict with
`jj`, then run `jj-stack submit <head-change-id>`.

If another local path still depends on an old merged change, its PR link and review branch remain
until that other stack is synced. Work completed before any later stop remains in place, and
rerunning `sync` continues from the current state.

`sync` does not otherwise rewrite history. If your stack simply drifted because `trunk()`
advanced without anything in your stack merging, rebase only the intended bottom-to-head path
with plain `jj`:

```bash
jj rebase -r '<bottom-change-id>::<head-change-id>' -o 'trunk()'
```

The bounded revset matters when the bottom change also has sibling descendants.

Use `jj-stack sync --dry-run <head-change-id>` to preview merged changes and any cleanup or
rebase. If a rebase is needed, the preview cannot show the resulting PR updates because the
rebased commits do not exist yet. The real `sync` computes those updates after the rebase.

`sync --all` checks every PR known to jj-stack, reconciles each affected local stack, and cleans
up merged reviews whose local changes are gone. Independent stacks continue if one is blocked.

## 8. Close a stack without merging it

GitHub may still group the PRs as a stack. Remove that grouping first; this leaves every pull
request open:

```bash
jj-stack unstack --dry-run <head-change-id>
jj-stack unstack <head-change-id>
```

When the GitHub grouping no longer matches one local path, use the stack number printed by the
diagnostic:

```bash
jj-stack unstack --stack <number>
```

Close the PRs on GitHub or name each one explicitly with `gh`:

```bash
gh pr close <pr>
```

The saved PR links remain in place, so `submit` will not silently reuse closed reviews and
`cleanup` can verify what it removes. Preview cleanup for only this local stack, then apply it:

```bash
jj-stack cleanup --dry-run <head-change-id>
jj-stack cleanup <head-change-id>
```

Cleanup keeps a review branch whenever another open PR still uses it as a base. Close or retarget
the PR named in the message, then rerun the same cleanup command.

If `jj-stack list` shows an `orphan` row, select it directly for closure and cleanup:

```bash
jj-stack cleanup --pull-request 7 --close --dry-run
jj-stack cleanup --pull-request 7 --close
```

To close and clean up every orphan shown by `list` at once:

```bash
jj-stack cleanup --pull-request orphans --close --dry-run
jj-stack cleanup --pull-request orphans --close
```

Already closed or merged PRs do not make `--close` fail; cleanup simply skips their closure.

Use `--local` only when you want this repository to forget its saved PR links while leaving
GitHub unchanged:

```bash
jj-stack unstack --local <head-change-id>
```

If `jj-stack list` says another tracked stack changed since its last submit, either run
`jj-stack submit <head-change-id>` to refresh the PR branches or run
`jj-stack view <head-change-id>` to inspect first. `view` also calls out which tracked changes in
the selected stack no longer match their last submitted commits, and whether
`jj-stack sync <head-change-id>` is needed first.

## Short version

The steady-state loop is:

```bash
jj-stack view
jj-stack submit
# edit in jj
jj-stack submit
jj-stack merge
```

## When something goes wrong

If a command is interrupted mid-way (crash, Ctrl-C, network failure), inspect the affected stack
first:

```bash
jj-stack view <head-change-id>
```

Then choose the recovery command based on what was interrupted:

```bash
# submit or unstack: rerun it with the same explicit selector
jj-stack submit <head-change-id>
jj-stack unstack <head-change-id>

# cleanup: preview and rerun it with the same selector
jj-stack cleanup --dry-run <head-change-id>
jj-stack cleanup <head-change-id>

# sync: retry the same mode explicitly
jj-stack sync --dry-run <head-change-id>
jj-stack sync <head-change-id>
jj-stack sync --all --dry-run
jj-stack sync --all

# merge completed on GitHub but automatic sync stopped: apply it; do not rerun merge
jj-stack sync --dry-run <head-change-id>
jj-stack sync <head-change-id>
```

Use explicit selectors after a failure, not a naked command that falls back to
the default stack. To start a review over, remove any GitHub stack grouping, close the old PRs,
clean up their branches and saved links, then `submit` again.

See the [troubleshooting guide](troubleshooting.md) for more recovery scenarios.
