# Troubleshooting

This page is organized by symptom and what you should do.

## A command says a revset resolved to more than one revision

The message is `Revset <x> resolved to more than one revision.` Your selector matched several
revisions, so `jj-stack` stops rather than guessing which stack you meant.

Pass a selector that names one revision — usually the stack's head change ID:

```bash
jj-stack view <head-change-id>
jj-stack submit <head-change-id>
```

## A command says a change has more than one mutable local copy

Concurrent `jj` workspace operations can leave two mutable copies of one change. `jj-stack`
cannot know which one you intend to review or rewrite, so selection stops.

Inspect the copies and abandon or reconcile the one you do not want:

```bash
jj log -r 'change_id(<change-id>)'
```

One mutable copy beside an immutable copy on fetched trunk is different: that is the ordinary
result of fetching a GitHub rebase merge, and the full change ID or linked PR selects the mutable
local copy.

## A command says a PR is claimed by multiple tracked records

The message is `PR #<n> is claimed by multiple tracked records (...)` or `PR #<n> is linked to
multiple local changes.` Two saved records point at the same pull request, so `jj-stack` cannot
tell which local change owns it. An explicit revset does not help here; the tracking has to be
repaired.

Find the records, then drop the wrong one or reattach it:

```bash
jj-stack list
jj-stack unstack --local <head-change-id>
jj-stack relink <pr> <change-id>
```

## `view` says it cannot find a trunk bookmark

Possible causes:

- the repo is brand new and does not have a trunk bookmark
- your main bookmark exists, but `trunk()` does not point to it
- you don't have a remote trunk branch set up

What to do:

- If you are working in a new repo, make some initial commits, create a `main` bookmark, and
  push your changes to GitHub. Once you've done all of this, you should have a working `trunk()`
  bookmark, and can rerun the `view` command.
- In an existing repo, configure `trunk()` to point to your trunk bookmark,
  such as `main`. For example:

```bash
jj config set --repo 'revset-aliases."trunk()"' main
```

## GitHub shows different PR state than `view` reports

Possible causes:

- the review branch moved or disappeared
- a PR link or review branch changed on another machine or workspace
- you want to refresh fetched repository state and check current review branches

What to do:

```bash
jj git fetch
jj-stack view
```

`view` always checks live GitHub state when GitHub is reachable, and always observes each saved
review branch directly on the remote — you only need the fetch to bring your local trunk up to
date. Ordinary fetch excludes the review branches: `jj-stack` adds that exclusion to the
remote's Git
fetch configuration the first time it needs the remote, and says so, which is what keeps the
review branches from being imported as persistent bookmarks. See the README for how to undo
it.

If a change shows `saved PR #<n>, no PR found for branch`, `jj-stack` remembers submitting that
change, but GitHub no longer reports pull request `#<n>` at all — it was deleted, the number is
wrong, or the repository moved. Run `jj-stack view <change-id>` first to confirm.

To start the review over, forget the local tracking and submit again:

```bash
jj-stack unstack --local <head-change-id>
jj-stack submit <head-change-id>
```

`unstack --local` only removes this repository's record of the PR and its last submitted commit;
it contacts neither GitHub nor the remote, which is what makes it work when the PR is gone.
`submit` then opens a fresh PR. `unstack --cleanup` is not the command here: it tries to close the
PR first, and stops on this change because GitHub no longer reports one.

If the change instead shows `remembered PR #<n>`, the PR does still exist. `view` prints an
advisory when it has moved to another head branch, and `jj-stack relink <pr> <change-id>` points
the change at it.

If GitHub reports a remembered PR as closed or merged, decide what outcome you
want before choosing a command:

- To keep reviewing the same PR, reopen it on GitHub and rerun `jj-stack view <change-id>`.
- To attach a different open PR to the change, use `jj-stack relink <pr> <change-id>`. That PR
  must be open and its head must already be the review branch for that same change.
- To abandon the old review and make fresh PRs, run `jj-stack unstack --cleanup <head-change-id>`
  and then `jj-stack submit <head-change-id>`. `relink` is not the right command for that case
  because it attaches an existing open PR.

## Lower changes merged elsewhere and the rest of your stack needs rebasing

Possible causes:

- some lower changes in your stack were merged on GitHub with different commit
  IDs, which can happen through e.g. a squash merge
- your local stack still contains those old commit IDs
- the remaining changes are still based on that old local history

What to do:

```bash
jj-stack sync <head-change-id>
```

`jj-stack sync <head-change-id>` verifies which lower PRs GitHub merged, rebases the remaining
changes above the current `trunk()`, and updates only PRs that already exist for them. Use
`jj-stack sync --dry-run <head-change-id>` first to preview merged changes and any cleanup or
rebase. If a rebase is needed, its later PR-update plan is available only after you run `sync`.
When a native merge rewrote the PRs that remain open, `sync` adopts those exact reviewed commits
and rebases only trailing local work above them. It leaves other stacks and unreviewed trailing
changes alone.

## Trunk advanced, but none of your stack merged

`sync` is for reconciling GitHub merge results. If `trunk()` merely moved forward, rebase the
selected local path with `jj`:

```bash
jj rebase -r '<bottom-change-id>::<head-change-id>' -o 'trunk()'
```

Use the bounded bottom-to-head revset so sibling paths are not rewritten. Then run
`jj-stack submit <head-change-id>` to refresh the existing PRs.

## `sync` rebased the stack but reported conflicts

The local rebase happened, but `jj-stack` did not update conflicting changes on GitHub. Inspect
the conflicts with `jj status`, resolve them using your normal `jj` workflow, and then run:

```bash
jj-stack submit <head-change-id>
```

## `list` or `view` says another stack changed since its last submit

`list` checks every stack known to local tracking. It does not discover GitHub-only stacks.
`view` checks another locally tracked stack only when that stack is built on top of a change in
the stack you are inspecting.

Possible causes:

- you amended, described, or otherwise rewrote a reviewed change without moving it
- you inserted a new change into a reviewed stack
- you abandoned, reordered, or rebased changes that already have PRs
- a stack that you are not currently looking at now has different parent relationships

What to do:

```bash
jj-stack submit <head-change-id>
```

Use the head change ID printed in the warning. To inspect first, run:

```bash
jj-stack view <head-change-id>
```

`view` and `list` compare each tracked change against the commit last submitted for it, and name
the ones that no longer match. `submit` refreshes that stack's PR branches and base branches on
GitHub so reviewers see the current local stack.

## `merge` stops because the change, its submitted commit, and its branch differ

Possible causes:

- you rewrote the change after submitting it, so the local commit no longer matches the one sent
  for review
- the review branch on the remote was moved, or a submit did not finish, so it no longer holds the
  commit recorded for that change

What to do:

```bash
jj-stack submit <head-change-id>
```

If you want to notify prior reviewers again after updating the PR, follow with:

```bash
jj-stack submit --re-request
```

GitHub reports the related case separately. If the pull request's head moved on GitHub while the
merge was in flight, `merge` says the PR head changed and names the same `submit` command; nothing
local has to change first.

A pure rebase with the same diff still changes the reviewed commit identity. Rerun `submit` so
the review branch, PR, and `jj-stack` tracking all name that exact commit before merging.

## GitHub rejects `merge`

Possible causes:

- required checks are pending or failing
- a review or repository rule is not satisfied
- the changes conflict
- a ruleset requires a merge queue
- your account cannot merge the pull request

`jj-stack` does not judge any of these itself; it asks GitHub and reports the answer, quoting
GitHub's own reason.

What to do:

- **If the changes conflict** — GitHub says something like `Pull Request is not mergeable` —
  rerunning `merge` cannot help, because the reviewed commit is the problem. Rebase onto the
  current trunk, resolve, resubmit, and merge again:

  ```bash
  jj rebase -r '<bottom-change-id>::<head-change-id>' -o 'trunk()'
  jj-stack submit <head-change-id>
  jj-stack merge <head-change-id>
  ```

  `submit` is required: the rebase gives every change a new commit ID, and `merge` only accepts
  the exact commit last sent for review.
- For any other reason GitHub gives — a pending or failing check, an unsatisfied review or
  repository rule — fix that condition on GitHub, then rerun the same explicit `merge` command.
  Nothing local needs to change.
- For an identical GitHub stack request already in progress, wait and rerun. Once it completes,
  the retry observes the terminal result.
- A failed GitHub stack operation merges nothing. An ordinary bottom-up merge can leave lower PRs
  merged before a later one is rejected; run `jj-stack sync <head-change-id>`, then retry
  `jj-stack merge <head-change-id>` if you still want the remainder.
- `jj-stack` does not enqueue merge-queue work. If repository policy requires a queue, use the
  repository's supported queue workflow, then run `jj-stack sync <head-change-id>` once GitHub
  merges the work.
- An access-denied response is a permissions problem. Fix repository permissions before retrying.

## PRs for this stack exist on GitHub but `jj-stack` doesn't know about them

Possible causes:

- the stack was submitted from a different machine or workspace
- you cloned the repo and want to pick up review work that is already in progress

What to do:

```bash
jj-stack checkout --pull-request <pr> --fetch
```

Use `checkout` when the problem is "these PRs exist on GitHub but I can't manage them locally
yet." It connects the PRs to local tracking and prints their tip without moving the working copy.
To continue from that tip:

```bash
jj new <tip-commit-id>
```

Use `jj-stack checkout --pick` only for stacks this local repository already tracks; to discover
a GitHub-only stack, select one of its PRs explicitly as shown above.

If `checkout` instead reports that the change is already here at a different commit, this
repository already has the change and you have edited it since the last submit. Fetching would
leave two copies of it, so `checkout` stops. Attach the pull request to the change you already
have, then publish your edit:

```bash
jj-stack relink <pr> <change-id>
jj-stack submit
```

For a stack of several PRs, relink attaches the one you name; rerun `jj-stack submit` and follow
the guidance it prints for any remaining untracked branch.

## `submit` says a GitHub stack keeps other PRs active outside the selected stack

GitHub still groups PRs that your local `jj` history now places on separate paths. `jj-stack`
stops because updating only part of that GitHub group would be unsafe.

Run the exact command from the diagnostic, which has this form:

```bash
gh stack unstack <number>
```

Then submit each local path separately. If `gh stack` is unavailable, install GitHub's extension:

```bash
gh extension install github/gh-stack
```

## Old review branches remain after merging or closing

Possible causes:

- your `unstack` succeeded, but the follow-up cleanup hasn't run yet
- GitHub merged the PRs, but `jj-stack sync` or a later `cleanup` has not run yet
- another visible stack still needs the saved review link
- `cleanup` could not confirm that a branch or comment is unused, so it left it alone and said so

What to do:

```bash
jj-stack sync <head-change-id>
jj-stack cleanup --dry-run # optional
jj-stack cleanup
```

Run `jj-stack sync <head-change-id>` first when merged changes still appear in the local stack.
Use `cleanup --dry-run` to preview any remaining branch, comment, or tracking removal, then
run plain `cleanup` to apply the listed actions.

## A command reports an imported review bookmark

`jj-stack` reserves one whole branch namespace — `jj-stack/` unless you set `branch_prefix` —
and its fetches exclude it. This diagnostic means a manual or non-isolated fetch imported a
bookmark from that namespace, or a leftover backing Git ref was exposed during a `jj-stack`
operation. Such a bookmark can make a review change immutable or ambiguous, which is why any name
in the namespace is reported and not only the names `jj-stack` generates.

Move any local work to a bookmark outside the namespace, then forget the imported bookmark with
the exact `jj bookmark forget --include-remotes ...` command from the diagnostic. Run the
printed `jj git export` command next so the backing Git ref is also removed, then rerun
`jj-stack`.

If the diagnostic instead names an effective `remotes.<remote>.fetch-bookmarks` override, unset
that exact setting first so `jj-stack` can keep the managed namespace isolated.

If the reservation itself is missing — the case where an ordinary fetch could import the
namespace in the first place — `jj-stack doctor --fix` restores it.

## Another jj-stack operation is already running

`jj-stack` takes one lock per repository so two mutating commands cannot interleave. If another
`jj-stack` is genuinely running, wait for it to finish.

A variant of the message says the recorded holder is no longer running. That means the record of
who took the lock is stale while the lock itself is still held. The operating system holds the
lock and drops it when the owning process exits, so waiting a moment and rerunning the command is
the whole fix. Do not delete anything by hand.

## You want to stop reviewing a stack on GitHub

Cause:

- your work was abandoned, replaced, or is no longer meant for review

What to do:

```bash
jj-stack unstack --dry-run
jj-stack unstack
```

If you already know the pull request number, you can use:

```bash
jj-stack unstack --pull-request 7 --dry-run
jj-stack unstack --pull-request 7
```

This closes the stack's pull requests but keeps their exact tracking and submitted commits. That
lets later cleanup verify the old artifacts and prevents `submit` from silently reusing a closed
review. Add `--cleanup` if you also want to delete review branches, comments, and
tracking that `jj-stack` can verify are safe to remove.

Plain `jj-stack cleanup` handles closed or merged reviews and leaves open reviews alone. If it
reports that another open PR depends on a review branch, close or retarget the named PR and rerun
the same cleanup command.

## A command was interrupted before it finished

Possible causes:

- `submit` or another mutating command was cut short (Ctrl-C, crash, power or network failure)
  after it had already done some work but before it finished

First, check the repository itself. `doctor` reports leftovers from an interrupted `checkout` or
`sync`, the state of the review-branch fetch reservation, remote and trunk resolution, and GitHub
authentication, and names a recovery command for what it finds:

```bash
jj-stack doctor
```

It changes nothing on GitHub. Add `--fix` to let it apply the local repairs it can make safely.

Then inspect the stack:

```bash
jj-stack view
```

If you know which stack was being changed, inspect it directly:

```bash
jj-stack view <head-change-id>
```

### Finish what was started

Use the stack's head change ID so you do not accidentally operate on another stack or only on a
prefix of the affected stack. `jj-stack` rereads current jj, tracking, remote, and GitHub state
each time.

- `submit`: preview with `jj-stack submit --dry-run <head-change-id>`, then run
  `jj-stack submit <head-change-id>`.
- `unstack` or `unstack --cleanup`: add `--dry-run` to the same explicit command, inspect it,
  then rerun without `--dry-run`.
- `sync`: preview with `jj-stack sync --dry-run <head-change-id>`, then run
  `jj-stack sync <head-change-id>`.
- `sync --all`: preview with `jj-stack sync --all --dry-run`, then run
  `jj-stack sync --all`.
- `merge`: rerun the same explicit selector and merge method. A matching request still in progress
  asks you to wait; a completed native request is observed on retry. If an ordinary merge accepted
  lower PRs first, preview with `jj-stack sync --dry-run <head-change-id>`, then run
  `jj-stack sync <head-change-id>` before retrying the remainder.

`jj-stack sync <head-change-id>` handles commits rewritten by GitHub while keeping a review
branch that a PR above still needs. `sync --all` checks independently tracked exact commits
already on trunk. Both inspect current GitHub state and trunk history.

### Back out

```bash
jj-stack unstack --cleanup --dry-run <head-change-id>
jj-stack unstack --cleanup <head-change-id>
```

If a failed `submit` created PRs or review branches that you no longer want, run
`unstack --cleanup` on the selected stack. If the change was abandoned and only tracking data
remains, use `jj-stack list` to find the orphaned PR and then
`jj-stack unstack --cleanup --pull-request <pr>`. To clean up every orphan shown by
`jj-stack list`, preview `jj-stack unstack --cleanup --pull-request orphans --dry-run`, then
run it again without `--dry-run`.
