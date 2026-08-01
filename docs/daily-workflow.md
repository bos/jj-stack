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

To review and polish those titles and bodies in your editor before anything is pushed:

```bash
jj-stack submit --edit
```

Saving the document continues the submit; quitting the editor with a non-zero exit aborts it
with nothing pushed.

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

This matters for merging: `jj-stack merge` skips a draft PR and everything above it, so a draft
left at the bottom of the stack blocks the whole merge.

## 4. Revise locally as reviews come in

During review, you can make any changes you want with `jj`. Split, squash, reorder, or rewrite
locally as needed.

Once the local stack looks right again, refresh GitHub:

```bash
jj-stack submit
```

If a rewrite splits, moves, or combines changes from existing GitHub stacks, `submit` may tell
you that an existing GitHub stack no longer matches the selected local path. Run every exact
`gh stack unstack <number>` command in that diagnostic to dissolve the old grouping, then submit
each resulting local stack. If `gh stack` is unavailable, install GitHub's extension first:

```bash
gh extension install github/gh-stack
```

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
jj-stack checkout --pull-request <pr> --fetch
```

Despite its name, `checkout` does not move the working copy. It fetches only the exact reviewed
commits needed to identify the stack, saves local tracking, and prints the tip commit. It does not
leave persistent review bookmarks behind. To continue on top of those reviewed commits, use
`jj new <tip-commit-id>` afterward; to edit an existing change directly, use
`jj edit <change-id>`.

When several stacks are already tracked in this repository and you do not remember a head change
ID, `jj-stack checkout --pick` presents a numbered list. It does not discover GitHub-only stacks.

If you want to inspect the stack for one linked PR directly:

```bash
jj-stack view --pull-request 7
```

(You can use `-p` as an alias for `--pull-request`.)

A full change ID and a linked PR continue to select the mutable local copy after you fetch an
ordinary GitHub rebase merge. If two mutable copies of that change are visible, `jj-stack` stops;
use `jj log -r 'change_id(<change-id>)'` to inspect them and choose or reconcile the copy you
want.

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
try to duplicate GitHub's rules for approvals, checks, conflicts, merge queues, or repository
policy. GitHub evaluates those rules when it handles the request.

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

The merge method comes from repository settings when only one is enabled. When several are, set a
default once:

```bash
jj config set --repo jj-stack.merge_method squash
```

Or choose one per run, which overrides that default:

```bash
jj-stack merge --method squash
```

GitHub reports which methods a repository allows but never which one to prefer, which is why one
of these is needed.

GitHub merges a selected multi-PR bottom portion as one operation. A failed operation merges
nothing. GitHub may rewrite the branches for PRs that remain above a partial selection. A one-PR
review uses GitHub's ordinary pull-request merge API.

`merge` does not rewrite local history, refresh surviving PRs, or remove tracking. After GitHub
merges anything, run the `jj-stack sync <head-change-id>` command printed in the result. If an
identical stack request is already pending, wait and rerun the same explicit `merge` command; once
GitHub
finishes, the retry observes the completed result.

## 7. Update a stack after GitHub merged lower PRs

Use `sync` with the stack's head change ID after `merge`, or whenever GitHub merged lower PRs
through different commit IDs and your local stack still contains the old commits:

```bash
jj-stack sync <head-change-id>
```

`sync` fetches trunk, verifies which lower PRs GitHub merged, rebases the remaining selected
changes, and updates only PRs that already exist for them. It does not open a PR for trailing WIP
or touch other local stacks. If it cannot safely remove an old local copy, it leaves the change
alone and prints the commits and inspection step that explain the stop.

GitHub may preserve a change as it merges or create a different commit, as a squash merge does.
`sync` handles either result without pretending the new GitHub commit is the old local change.
If a stack merge also rewrote the PRs that remain open, `sync` adopts those exact reviewed
commits and rebases only your trailing local work above them.

If reviewed work is already on fetched trunk but its local copy still follows unmerged changes,
`sync` cannot choose their intended order. It changes nothing and prints the earlier change IDs,
the submitted, local, and fetched-trunk commits, and an exact `jj log` command. Inspect that
history and choose the order with ordinary `jj`; ask an agent to inspect the repository if useful.
Then run `jj-stack view` for the remaining local reviews. Sync a remaining mutable reviewed head,
or run `jj-stack cleanup` if no reviewed local copy remains.

`sync` does not otherwise rewrite history. If your stack simply drifted because `trunk()`
advanced without anything in your stack merging, rebase only the intended bottom-to-head path
with plain `jj`:

```bash
jj rebase -r '<bottom-change-id>::<head-change-id>' -o 'trunk()'
```

The bounded revset matters when the bottom change also has sibling descendants.

Use `jj-stack sync --dry-run <head-change-id>` to preview merged changes and any cleanup or
rebase. When a rebase is needed, the later PR-update plan is available only after you run `sync`.
`sync --all` checks every locally tracked PR and cleans up those whose exact submitted commits are
already on trunk. It may retarget and close those PRs and remove their tracking data, but it never
rewrites or submits a stack. When GitHub created a different commit, `sync --all` leaves tracking
in place and prints a
`jj-stack sync <head-change-id>` command for each affected stack.

## 8. Unstack abandoned stacks

If a stack should no longer be reviewed, preview which PRs will close and then apply:

```bash
jj-stack unstack --dry-run
jj-stack unstack
```

If it's handier to identify your stack by PR number, you can specify that instead:

```bash
jj-stack unstack --pull-request 7 --dry-run
jj-stack unstack --pull-request 7
```

Plain `unstack` closes the PRs but retains their exact tracking and submitted commits. That
information prevents a later `submit` from silently reusing a closed review and lets `cleanup`
verify what it is acting on.

Use `--cleanup` when you also want to remove review branches, comments, and tracking that
`jj-stack` can verify are safe to delete after the PRs close.

Cleanup keeps a review branch and its tracking whenever any open PR in the same GitHub repository
still uses that branch as its base, even if that PR is not tracked by `jj-stack` or its local
change is gone. Close or retarget the dependent PR named in the blocker, then rerun the same
cleanup command. For a selected stack, `jj-stack` works from the head down; a preview may account
for upper selected PRs it would close first, while the real command checks GitHub again before
each deletion.

Use `--local` when you only want this local repository to stop tracking the stack. It removes the
exact local PR and submitted-commit records while leaving the PRs and review branches alone:

```bash
jj-stack unstack --local
```

If `jj-stack list` shows an `orphan` row, tracking remains for a PR whose local change is no
longer part of any current stack. When you are ready, preview closing it if needed and cleaning
up its verified review artifacts:

```bash
jj-stack unstack --cleanup --pull-request 7 --dry-run
jj-stack unstack --cleanup --pull-request 7
```

To preview and clean up every orphan shown by `list` in one operation, run:

```bash
jj-stack unstack --cleanup --pull-request orphans --dry-run
jj-stack unstack --cleanup --pull-request orphans
```

If GitHub groups the selected PR with other active PRs that must close together, both the preview
and real command stop before changing anything unless the full group belongs to the selected
local path.

If `jj-stack list` says another tracked stack changed since its last submit, either run
`jj-stack submit <head-change-id>` to refresh the PR branches or run
`jj-stack view <head-change-id>` to inspect first. `view` only emits this warning for another
stack when its local path shares a change with the stack you are inspecting, such as two paths
created by splitting above a reviewed change. A stack that only shares trunk stays silent.
`view` also calls out which tracked changes no longer match their last submitted commits, and
whether `jj-stack sync <head-change-id>` is needed first.

## Short version

The steady-state loop is:

```bash
jj-stack view
jj-stack submit
# edit in jj
jj-stack submit
jj-stack merge
jj-stack sync <head-change-id>
```

Use the head change ID printed by `merge`.

## When something goes wrong

If a command is interrupted mid-way (crash, Ctrl-C, network failure), inspect the affected stack
first:

```bash
jj-stack view <head-change-id>
```

Then choose the recovery command based on what was interrupted:

```bash
# submit or plain unstack: rerun it with the same explicit selector
jj-stack submit <head-change-id>
jj-stack unstack <head-change-id>

# if the interrupted command was unstack --cleanup, keep that explicit option
jj-stack unstack --cleanup <head-change-id>

# sync: retry the same mode explicitly
jj-stack sync --dry-run <head-change-id>
jj-stack sync <head-change-id>
jj-stack sync --all --dry-run
jj-stack sync --all

# merge after GitHub accepted one or more PRs: reconcile, then retry if desired
jj-stack sync --dry-run <head-change-id>
jj-stack sync <head-change-id>
jj-stack merge <head-change-id>
```

Use explicit selectors after a failure, not a naked command that falls back to
the default stack. If you want to undo review work that was partially created,
use `unstack --cleanup` on the stack you want to close and clean up. That is also how you start
a review over from scratch: close and clean up the old PRs, then `submit` again.

See the [troubleshooting guide](troubleshooting.md) for more recovery scenarios.
