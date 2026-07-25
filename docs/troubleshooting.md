# Troubleshooting

This page is organized by symptom and what you should do.

## `view` or `submit` says the stack selection is ambiguous

Possible causes:

- the current repo state doesn't resolve to one clear stack
- the remote or trunk branch is configured in an unusual way
- the revset you passed doesn't point at what you expected

What to do:

```bash
jj-stack view
```

If needed, pass an explicit revset:

```bash
jj-stack view <revset>
jj-stack submit <revset>
```

For safety, `jj-stack` always stops and reports what is ambiguous rather than guessing what you
might have meant.

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
jj-stack view --fetch
```

`view` already checks live GitHub state when GitHub is reachable. `view --fetch` also refreshes
ordinary fetched repository state and directly observes each saved review branch. Ordinary fetch
excludes `review/*`, so this read-only refresh does not import the review branches as persistent
bookmarks.

If a change shows `submitted, no PR found for branch`, `jj-stack` has tracking
for a previous submit, but GitHub did not report a PR for the current review
branch. Run `jj-stack view --fetch <change>` first. If the PR is still open
under a different branch or tracking record, use `jj-stack relink <pr> <change>`.
If no open PR exists and you want fresh PRs, run:

```bash
jj-stack submit --restart <stack-head>
```

`--restart` requires complete tracking for every selected change. It keeps all old tracking until
the whole replacement stack succeeds. If the command is interrupted after creating some PRs,
rerun the same command with the same stack head; `jj-stack` reuses only replacement PRs whose
branch, commit, and base still exactly match the plan.

If GitHub reports a remembered PR as closed or merged, decide what outcome you
want before choosing a command:

- To keep reviewing the same PR, reopen it on GitHub and rerun `jj-stack
  view --fetch <change>`.
- To attach a different open PR to the change, use `jj-stack relink <pr>
  <change>`.
- To abandon the old review and make fresh PRs, run `jj-stack submit
  --restart <stack-head>`. `relink` is not the right command for that case
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

Selected `sync` verifies which lower PRs GitHub merged, rebases the selected remaining changes
above the current `trunk()`, and updates only PRs that already exist for them. Use
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

Status identifies tracked changes whose current commits no longer match the last successful
submit. `submit` refreshes that stack's PR branches and base branches on GitHub so reviewers see
the current local stack.

## `merge` says the local change differs from what reviewers approved

Possible causes:

- you submitted a change, it got reviewed and approved, and meanwhile you rewrote it in a way
  that changed its diff
- the PR branch on GitHub still shows the older reviewed content

What to do:

```bash
jj-stack submit
```

If you want to notify prior reviewers again after updating the PR, follow with:

```bash
jj-stack submit --re-request
```

A pure rebase with the same diff still changes the reviewed commit identity. Rerun `submit` so
the review branch, PR, and `jj-stack` tracking all name that exact commit before merging.

## GitHub rejects `merge`

Possible causes:

- required checks are pending or failing
- a review or repository rule is not satisfied
- the changes conflict
- a ruleset requires a merge queue
- your account cannot merge the pull request

What to do:

- Read GitHub's reason in the error. Fix that condition, then rerun the same explicit `merge`
  command.
- For an identical GitHub stack request already in progress, wait and rerun. Once it completes,
  the retry observes the terminal result.
- A failed GitHub stack operation merges nothing. An ordinary bottom-up merge can leave lower PRs
  merged before a later one is rejected; run `jj-stack sync <head-change-id>`, then retry
  `jj-stack merge <head-change-id>` if you still want the remainder.
- `jj-stack` does not enqueue merge-queue work. If repository policy requires a queue, use the
  repository's supported queue workflow, then run selected `sync` after GitHub merges the work.
- An authorization rejection is an access problem. Fix repository permissions before retrying.

## PRs for this stack exist on GitHub but `jj-stack` doesn't know about them

Possible causes:

- the stack was submitted from a different machine or workspace
- you cloned the repo and want to pick up review work that is already in progress

What to do:

```bash
jj-stack checkout --pull-request <number-or-url> --fetch
```

Use `checkout` when the problem is "these PRs exist on GitHub but I can't manage them locally
yet." It connects the PRs to local tracking and prints their tip without moving the working copy.
To continue from that tip:

```bash
jj new <tip-commit-id>
```

Use `jj-stack checkout --pick` only for stacks this local repository already tracks; to discover
a GitHub-only stack, select one of its PRs explicitly as shown above.

## `submit` says one GitHub stack spans several local paths

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
- GitHub merged the PRs, but selected `sync` or later cleanup has not run yet
- another visible stack still needs the saved review link
- something prevented conservative cleanup from proving that an artifact is safe to remove

What to do:

```bash
jj-stack sync <head-change-id>
jj-stack cleanup --dry-run # optional
jj-stack cleanup
```

Run selected `sync` first when merged ancestors still appear in the local stack. Use
`cleanup --dry-run` to preview any remaining branch, comment, or tracking removal, then
run plain `cleanup` to apply the listed actions.

## A command reports an imported managed review bookmark

Normal `jj-stack` fetches exclude `review/*`. This diagnostic means a manual or non-isolated fetch
imported a managed review branch into the local bookmark view, where it could make a review change
immutable or ambiguous.

Move any local work to a bookmark outside `review/`, then forget the imported managed bookmark
with the exact `jj bookmark forget --include-remotes <review/...>` command from the diagnostic
and rerun `jj-stack`. If the diagnostic instead names an effective
`remotes.<remote>.fetch-bookmarks` override, unset that exact setting first so `jj-stack` can keep
the managed namespace isolated.

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

First, inspect the stack:

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
- `submit --restart`: rerun `jj-stack submit --restart <head-change-id>`. Exact replacement PRs
  from the interrupted run are reused; the old tracking remains until the whole stack succeeds.
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

This backout does not apply to an interrupted `submit --restart`: its replacement PRs are not
tracked until the whole restart succeeds, so `unstack` would still select the old reviews. Rerun
the same restart instead.
