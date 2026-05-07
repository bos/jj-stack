# Troubleshooting

This page is organized by symptom and what you should do.

## `status` or `submit` says the stack selection is ambiguous

Possible causes:

- the current repo state doesn't resolve to one clear stack
- the remote or trunk branch is configured in an unusual way
- the revset you passed doesn't point at what you expected

What to do:

```bash
jj-review status
```

If needed, pass an explicit revset:

```bash
jj-review status <revset>
jj-review submit <revset>
```

For safety, `jj-review` always stops and reports what is ambiguous rather than guessing what you
might have meant.

## `status` says it cannot find a trunk bookmark

Possible causes:

- the repo is brand new and does not have a trunk bookmark
- your main bookmark exists, but `trunk()` does not point to it
- you don't have a remote trunk branch set up

What to do:

- If you are working in a new repo, make some initial commits, create a `main` bookmark, and
  push your changes to GitHub. Once you've done all of this, you should have a working `trunk()`
  bookmark, and can rerun the `status` command.
- In an existing repo, configure `trunk()` to point to your trunk bookmark,
  such as `main`. For example:

```bash
jj config set --repo 'revset-aliases."trunk()"' main
```

## GitHub shows different PR state than `status` reports

Possible causes:

- the local bookmark tracking the remote branch is out of date
- a PR link or review branch changed on another machine or workspace
- you want to refresh both live GitHub state and local remote-bookmark observations

What to do:

```bash
jj-review status --fetch
```

`status` already checks live GitHub state when GitHub is reachable. `status
--fetch` also refreshes remembered remote bookmark state before reporting, so
it is the safer read-only refresh when a PR link, branch state, or merged-base
relationship may have changed elsewhere.

If a change shows `submitted, no PR found for branch`, `jj-review` has tracking
for a previous submit, but GitHub did not report a PR for the current review
branch. Run `jj-review status --fetch <change>` first. If the PR is still open
under a different branch or tracking record, use `jj-review relink <pr> <change>`.
If no open PR exists and you want fresh PRs, run:

```bash
jj-review submit --restart <stack-head>
```

If GitHub reports a remembered PR as closed or merged, decide what outcome you
want before choosing a command:

- To keep reviewing the same PR, reopen it on GitHub and rerun `jj-review
  status --fetch <change>`.
- To attach a different open PR to the change, use `jj-review relink <pr>
  <change>`.
- To abandon the old review and make fresh PRs, run `jj-review submit
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
jj-review cleanup --rebase
jj-review submit
```

`cleanup --rebase` drops those merged ancestors from the active local stack
and rebases the remaining changes above the current `trunk()`. After that,
`submit` refreshes the open PRs to reflect the new base.

## `list` or `status` says another stack changed since its last submit

`list` checks every tracked stack in the repo. `status` only checks another stack when that stack
is built on top of a change in the stack you are inspecting.

Possible causes:

- you amended, described, or otherwise rewrote a reviewed change without moving it
- you inserted a new change into a reviewed stack
- you abandoned, reordered, or rebased changes that already have PRs
- a stack that you are not currently looking at now has different parent relationships

What to do:

```bash
jj-review submit <head-change-id>
```

Use the head change ID printed in the warning. To inspect first, run:

```bash
jj-review status <head-change-id>
```

Status reports what changed since the last successful submit: local commits, review parents, or
stack membership. `submit` refreshes that stack's PR branches and base branches on GitHub so
reviewers see the current local stack.

## `land` says the local change differs from what reviewers approved

Possible causes:

- you submitted a change, it got reviewed and approved, and meanwhile you rewrote it in a way
  that changed its diff
- the PR branch on GitHub still shows the older reviewed content

What to do:

```bash
jj-review submit
```

If you want to notify prior reviewers again after updating the PR, follow with:

```bash
jj-review submit --re-request
```

A pure rebase with the same diff does not need this. In that case, `land` will refresh the review
branch automatically before pushing `trunk()`.

## PRs for this stack exist on GitHub but `jj-review` doesn't know about them

Possible causes:

- the stack was submitted from a different machine or workspace
- you cloned the repo and want to pick up review work that is already in progress

What to do:

```bash
jj-review import --pull-request <number-or-url> --fetch
```

Use `import` when the problem is "these PRs exist on GitHub but I can't manage them locally
yet." This command is *not* for rewriting history or changing what is in the stack, only for
telling `jj-review` which local changes go with which PRs.

## Old review branches are still around after landing or closing

Possible causes:

- your `land` or `close` succeeded, but the follow-up cleanup hasn't run yet
- you ran `land --skip-cleanup` to keep the review branches on purpose
- something prevented `jj-review` from cleaning up automatically

What to do:

```bash
jj-review cleanup --dry-run # optional
jj-review cleanup
```

Use `--dry-run` if you want first, to preview what it plans to remove. Then run plain `cleanup`
to delete the old review branches, local review bookmarks, and saved review tracking data it
described.

## You want to stop reviewing a stack on GitHub

Cause:

- your work was abandoned, replaced, or is no longer meant for review

What to do:

```bash
jj-review close
```

If you already know the pull request number, you can use:

```bash
jj-review close --pull-request 7
```

This closes the stack's pull requests. Add `--cleanup` if you also want to delete the review
branches and clean up local tracking data for that stack. As usual, `--dry-run` will preview
what the command will do without actually taking action.

## A command was interrupted before it finished

Possible causes:

- `submit` or another mutating command was cut short (Ctrl-C, crash, power or network failure)
  after it had already done some work but before it finished
- `status` reports an interrupted operation

First, see what was interrupted:

```bash
jj-review status
```

`status` names the command that was cut short and the stack it was working on. From there you
have two options: **finish what was started**, or **back out**.

Interrupted-operation lines include when the command started. Recent notices usually mean a
command failed or was interrupted during your current work; older notices usually mean leftover
state from a previous day. If `status` says the interrupted operation is not for the stack shown
above, start by inspecting the printed change ID:

```bash
jj-review status <change-id>
```

### Finish what was started

Re-run the same command, passing the change ID or revset `status` printed so you don't
accidentally operate on a different stack. `jj-review` picks up where it left off and skips the
work that already completed.

| If `status` says was interrupted | Re-run                                   |
| -------------------------------- | ---------------------------------------- |
| `submit`                         | `jj-review submit <revset>`              |
| `close` / `close --cleanup`      | `jj-review close [--cleanup] <revset>`   |
| `cleanup --rebase`               | `jj-review cleanup --rebase <revset>`    |
| `land`                           | `jj-review land <revset>`                |

For an interrupted `land` specifically: if the trunk push already succeeded before the
interruption, the landed commits are already on `trunk()`. A rerun here just finishes the
post-land bookkeeping (closing PRs, forgetting local review bookmarks).

### Back out with `abort`

```bash
jj-review abort --dry-run   # preview
jj-review abort             # apply
```

What `abort` actually does depends on which command was interrupted:

- **`submit`**: closes any PRs it created, deletes the corresponding remote branches, forgets
  the local bookmarks, and clears the tracking entries. This is the only case where `abort`
  performs a real undo.
- **`close`**: clears the interrupted-operation notice. It does **not** reopen PRs that were
  already closed.
- **`cleanup --rebase`**: clears the interrupted-operation notice. It does **not** restore the
  old local history. Use `jj op restore` if you want to undo the rebase itself.
- **`land`**: clears the interrupted-operation notice. It **cannot** un-merge changes that
  already reached `trunk()`.

If you want to fully back out one of the latter three, you have to do it by hand; `abort` is
only a true reverse for `submit`.

### `abort` refuses because the stack has changed

If you rewrite or reorder the stack after a `submit` was interrupted, `abort` will not try to
guess which PRs or review branches came from that interrupted submit. In that case you have two
options:

- **Finish the submit**: re-run `submit <change-id-from-status>` or another explicit revset for
  the stack you want. It detects any review branches or PRs that already exist, and completes
  whatever is still outstanding for that stack.
- **Undo the partial work**: run `jj-review close --cleanup <change-id-from-status>` or another
  explicit revset for that stack. A successful `close --cleanup` closes the open PRs, deletes
  the review branches, and clears the interrupted `submit` record once the recorded review
  artifacts for that stack are gone.

If the change ID printed by `status` no longer exists, the original stack head has been
abandoned or otherwise dropped from visible history. In that case `abort` only clears the
interrupted-operation notice; it does not close PRs or delete review branches for that missing
stack. Run `jj-review abort --dry-run` to preview the change, then run `jj-review abort` to
clear the notice.
