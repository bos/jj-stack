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
result of fetching a GitHub rebase merge, and a change ID, including a short prefix, or a linked
PR selects the mutable local copy.

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
date. `jj-stack doctor --fix` can add an exclusion that keeps review branches from being imported
as persistent bookmarks. Other commands honor the repository's configured fetch selection
without changing it. See the README for how to undo the exclusion.

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
`submit` then opens a fresh PR. `cleanup` is not the command here because it cannot verify a PR
that GitHub no longer reports.

If the change instead shows `remembered PR #<n>`, the PR does still exist. `view` prints an
advisory when it has moved to another head branch, and `jj-stack relink <pr> <change-id>` points
the change at it.

If GitHub reports a remembered PR as merged, run `jj-stack sync <change-id>` to update the local
stack and retire tracking for the merged review.

If GitHub reports a remembered PR as closed, decide what outcome you want before choosing a
command:

- To keep reviewing the same PR, reopen it on GitHub and rerun `jj-stack view <change-id>`.
- To attach a different open PR to the change, use `jj-stack relink <pr> <change-id>`. That PR
  must be open and its head must already be the review branch for that same change.
- To abandon the old review and make fresh PRs, close the old PR on GitHub, run
  `jj-stack cleanup <head-change-id>`, and then run `jj-stack submit <head-change-id>`. `relink`
  is not the right command for that case because it attaches an existing open PR.

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

`sync` applies the completed GitHub merges to the selected local stack and refreshes the PRs that
remain. It verifies which lower PRs merged, rebases the remaining changes above the current
`trunk()`, and updates only PRs that already exist for them. Use
`jj-stack sync --dry-run <head-change-id>` first to preview merged changes and any cleanup or
rebase. If a rebase is needed, the preview cannot show the resulting PR updates because the
rebased commits do not exist yet. The real `sync` computes those updates after the rebase.

When a stack merge rewrote the PRs that remain open, `sync` adopts those exact reviewed commits.
It rebases any later local work so it remains on top, but updates PRs only for the selected stack.

## Reviewed work is on trunk but an earlier local change remains

This can happen when you insert or reparent local work after submitting a review, then GitHub
merges the submitted commit before you refresh the review. `sync` does not guess whether your
local change should stay before the reviewed work or follow the commit now on trunk.

The stop names the earlier change IDs and the exact submitted, local, and fetched-trunk commits.
Run the printed `jj log` command to inspect both histories. Choose the intended order with your
normal `jj` workflow, or ask an agent with repository access to inspect those commit IDs and help
apply it.

After repairing the order, run `jj-stack view` to inspect the remaining local reviews. Run
`jj-stack sync <head-change-id>` for a remaining mutable reviewed head. If no reviewed local copy
remains, run `jj-stack cleanup` instead.

## Trunk advanced, but none of your stack merged

`sync` applies completed GitHub merges locally. If `trunk()` merely moved forward, rebase the
selected local path with `jj`:

```bash
jj rebase -r '<bottom-change-id>::<head-change-id>' -o 'trunk()'
```

Use the bounded bottom-to-head revset so sibling paths are not rewritten. Then run
`jj-stack submit <head-change-id>` to refresh the existing PRs.

## `submit --base` says the parent review is not an exact open snapshot

The reviewed parent is read-only input to a child submit. Its local commit, last submitted
commit, review branch, and live PR head must all match, and the PR must still be open.

If the parent changed locally, submit the parent review first, then repeat the child submit with
`--base`.

If the remote review branch moved or disappeared, `jj-stack` leaves it untouched and cannot
repair it automatically. This is a manual hard stop: externally restore the exact branch named
in the error to the immutable submitted commit ID that it prints, then repeat the exact child
submit command. Do not restore the branch to the parent's mutable change ID.

If the PR identity rather than its branch target moved, inspect the saved and live identities
before using `jj-stack relink`; do not restore a branch merely to make an unrelated PR match.

If the exact change named by `--base` has merged, sync its parent review and move exactly the
child range onto trunk instead. Do this even when a higher change in that review remains open:

```bash
jj-stack sync <parent-head-change-id>
jj rebase -r '<child-bottom-change-id>::<child-head-change-id>' -o 'trunk()'
jj-stack submit <child-head-change-id>
```

Do not pass `--base` to that last submit: the child is now an ordinary trunk-based review.

## `sync` rebased the stack but reported conflicts

`sync` can rebase a change that was already conflicted, and a clean change can become
conflicted during the rebase. In either case, the local rebase remains in place but `jj-stack`
does not update the conflicting review on GitHub. Inspect the conflicts with `jj status`, resolve
them using your normal `jj` workflow, and then run:

```bash
jj-stack submit <head-change-id>
```

## `sync` says another local stack still needs a merged change

Two local paths still depend on the same reviewed change. `sync` rebased the path you selected,
but it did not remove the shared change or its tracking because the other path still needs them.

Run each `jj-stack sync <head-change-id>` command printed by the message. After every dependent
path has moved to trunk, the last run can remove the old local change and its tracking.

## `list` says another stack changed since its last submit

`list` checks every stack known to local tracking. It does not discover GitHub-only stacks.

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
- GitHub rejected the direct merge or merge-queue request
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
- A failed GitHub stack operation merges nothing.
- If the trunk branch uses a merge queue, `merge` succeeds once GitHub accepts the selected PRs
  into it. This is not a merge failure and does not mean trunk changed. `view` and `list` show the
  PRs as queued; wait for GitHub to merge them before running `sync`.
- While a PR is queued, `submit` will not move its review branch or change the PR, and `sync`
  leaves that selected stack alone. Wait for it to merge. Other stacks remain usable.
- An access-denied response is a permissions problem. Fix repository permissions before retrying.

## `merge` completed on GitHub but could not update the local stack

The pull requests are already merged. Do not rerun `merge`: the command's nonzero exit describes
the later local sync failure, not a failed GitHub merge.

Follow the recovery instruction printed with the error. If it does not name a more specific
command, inspect and continue from current state with:

```bash
jj-stack view <head-change-id>
jj-stack sync <head-change-id>
```

No merge journal needs repairing. `sync` fetches again and derives the remaining work from
GitHub, trunk, the local `jj` DAG, and tracking.

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

## `submit` says to submit another local path first

You moved a change from one submitted stack into another. GitHub still groups the moved PR with
the source stack, so refreshing the destination first would require changing only part of that
group while also updating another submitted review.

Submit the remaining source stack first, then submit the destination stack. Both commands reuse
the existing PRs; you do not need to run `unstack` yourself.

At a reviewed fork, leave the fork in its parent review and submit each outgoing child with
`--base <fork-change-id> <child-head-change-id>`.

## Old review branches remain after merging or closing

Possible causes:

- you closed PRs on GitHub, but the follow-up cleanup hasn't run yet
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
Cleanup preserves their review branches and tracking so sync can still identify what landed. Use
`cleanup --dry-run` to preview any remaining branch, comment, or tracking removal, then run plain
`cleanup` to apply the listed actions.

## Review bookmarks are visible locally

`jj-stack` reserves one whole branch namespace — `jj-stack/` unless you set `branch_prefix`.
`jj-stack doctor --fix` configures ordinary fetches to exclude it. `doctor` warns when that
exclusion is missing, overridden, or review bookmarks are already visible.

A visible bookmark is not itself an error. If it exposes the exact commit saved for that review,
`jj-stack` accepts it. If a local rewrite has replaced that commit, the saved commit is treated as
the published version rather than another local change. The command still stops if the branch or
pull request on GitHub has moved, or if another `jj` rule makes the selected commit immutable.

An unknown bookmark is left untouched and does not block work on another stack. If it collides
with the name for a new review, move any local work to a bookmark outside the reserved namespace,
or forget the bookmark if it is only a stale imported copy. Then retry the command.

Use `jj-stack doctor --fix` to restore the normal fetch exclusion. If `doctor` names an effective
`remotes.<remote>.fetch-bookmarks` override, remove that setting if you want the exclusion to take
effect.

If `jj-stack list` says that a saved review branch is linked to several changes, it still shows
every stack but skips live details for the affected changes. Relink the PR to the change that owns
it, or clean up the stale saved review, then run `list` again.

## Another jj-stack operation is already running

`jj-stack` takes one lock per repository so two mutating commands cannot interleave. If another
`jj-stack` is genuinely running, wait for it to finish.

## You want to close a stack without merging it

Cause:

- your work was abandoned, replaced, or is no longer meant for review

What to do:

```bash
jj-stack unstack --dry-run <head-change-id>
jj-stack unstack <head-change-id>
```

`unstack` removes GitHub's stack grouping and leaves the PRs open. Close each PR on GitHub or
with `gh`:

```bash
gh pr close <pr>
```

If GitHub reports locked PRs, remove any queued PRs from the merge queue and rerun `unstack`.

The saved PR links remain in place. Remove the closed reviews' branches, comments, and saved links
with:

```bash
jj-stack cleanup --dry-run <head-change-id>
jj-stack cleanup <head-change-id>
```

If cleanup reports that another open PR depends on a review branch, close or retarget the named
PR and rerun the same command.

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
- `unstack`: add `--dry-run` to the same explicit command, inspect it, then rerun without
  `--dry-run`.
- `cleanup`: add `--dry-run` to the same revision or pull request selector, inspect it, then
  rerun without `--dry-run`.
- `sync`: preview with `jj-stack sync --dry-run <head-change-id>`, then run
  `jj-stack sync <head-change-id>`.
- `sync --all`: preview with `jj-stack sync --all --dry-run`, then run
  `jj-stack sync --all`.
- `merge`: rerun the same explicit selector and merge method only when GitHub did not complete the
  request. A matching request still in progress asks you to wait. If GitHub completed the merge
  but automatic sync stopped, do not rerun `merge`; follow the printed local recovery instead.

`jj-stack sync <head-change-id>` handles commits rewritten by GitHub while keeping a review
branch that a PR above still needs. `sync --all` checks independently tracked exact commits
already on trunk. Both inspect current GitHub state and trunk history.

### Remove an unwanted review

If a failed `submit` created PRs or review branches that you no longer want, remove any GitHub
stack grouping with `unstack`, close the PRs on GitHub, then preview and run
`jj-stack cleanup <head-change-id>`. If the local change is gone, use `jj-stack list` to find the
orphaned PR and select it with `jj-stack cleanup --pull-request <pr>`. After closing every orphan
shown by `list`, use `jj-stack cleanup --pull-request orphans` to remove their leftovers.
