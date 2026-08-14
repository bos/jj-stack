---
title: Troubleshooting
description: Occasionally you'll run into problems. Here's what to do.
navGroup: Fix a problem
weight: 120
---

Many `jj-stack` errors end with a `Hint:` that gives clear advice on what to do next. Start
there.

The examples below use `<head-change-id>` to identify your stack. If you do not know that ID, run
`jj-stack list` and copy your stack's head change ID.

## Setup or GitHub access fails

**How this can happen:** you just cloned your repository, its Git remote changed, or your GitHub
login expired.

Prepare your repository again and look for any failed checks:

```console
jj-stack doctor --fix
```

`doctor` checks your repository, trunk, Git remote, and GitHub access. It does not change anything
on GitHub.

## You merged pull requests on GitHub

**How this can happen:** you clicked Merge on GitHub, someone else merged your pull requests, or
a merge queue finished while you were away.

Run `sync` to apply the merge locally, update your remaining pull requests, and remove review
branches that are no longer needed:

```console
jj-stack sync <head-change-id>
```

If completed merges affected several stacks, or you do not want to identify each stack head, run:

```console
jj-stack sync --all
```

One blocked stack does not prevent jj-stack from syncing independent stacks.

If your pull requests are waiting in a merge queue, wait until GitHub reports them as merged
before running `sync`. (It's safe to run `sync` early; it just won't do anything.)

## You rebased your stack on GitHub

**How this can happen:** you used GitHub's **Rebase stack** action after trunk advanced.

Run `sync` after GitHub reports that the rebase completed:

```console
jj-stack sync <head-change-id>
```

`sync` verifies the rewritten contents, restores the original jj change IDs, and updates the same
review branches. If you changed the stack locally after submitting it, `sync` stops rather than
choosing between your local work and GitHub's result.

## `merge` did not merge your whole stack

**How this can happen:** `merge` works upward from the bottom of your stack. It stops when it
reaches a pull request that is a draft, no longer matches your local change, or cannot be merged
by GitHub.

The output identifies which of your pull requests it left open and explains why:

- If your local changes no longer match what you submitted, run
  `jj-stack submit <head-change-id>`, then retry `merge`.
- If GitHub reports a pending check, missing approval, draft pull request, repository rule, or
  permissions problem, fix that on GitHub, then retry the same `merge` command.
- If GitHub reports a conflict, rebase and resolve it with `jj`, submit the updated stack, then
  retry `merge`.
- If that pull request was already merged separately, run `jj-stack sync <head-change-id>`.

If all that happened was that trunk advanced, you may not need to rebase. GitHub can merge your
stack while its base is behind trunk when it has no conflicts.

## GitHub merged your stack, but `merge` ended with an error

**How this can happen:** GitHub completed the merge, then your network connection failed or
`jj-stack` could not finish updating your local repository.

Your pull requests are already merged, but your local update and GitHub cleanup may still need to
finish:

```console
jj-stack sync <head-change-id>
```

## A command was interrupted

**How this can happen:** you pressed Ctrl-C, closed the terminal, lost connectivity, or the
computer stopped while a command was running.

Check the current state before doing anything else:

```console
jj-stack view <head-change-id>
```

If GitHub completed a merge, finish off that work with `sync`. Otherwise, rerun the interrupted
command with the same head change ID.

## Your old review branches remain

**How this can happen:** your pull requests were merged or closed outside `jj-stack`, or cleanup
was interrupted.

If your pull requests were merged, run `sync` first. If they are closed and no longer needed, run
cleanup:

```console
jj-stack cleanup <head-change-id>
```

Cleanup leaves a review branch in place while an open pull request still needs it.

## “The selector resolved to more than one revision”

**How this can happen:** you passed a broad revset that identifies more than one of your changes
as a possible stack head.

Run `jj-stack list` to identify your intended stack, then rerun your failing command with that
stack's head change ID.

## “Divergent changes are not supported”

**How this can happen:** separate `jj` workspaces modified one of your changes independently,
leaving multiple local versions of its change ID. `jj-stack` cannot determine which version
belongs in your stack.

In this case, two or more of your local commits have the same `jj` change ID. Show both, then
compare their diffs:

```console
jj log -r 'change_id(<change-id>)'
jj diff -r <first-commit-id>
jj diff -r <second-commit-id>
```

Choose the version you want to keep, then abandon the other one by its commit ID:

```console
jj abandon <unwanted-commit-id>
```

> [!WARNING]
> Use the *commit IDs* to abandon an unwanted version of your change. Because divergent versions
> share a change ID, specifying the change ID would abandon *all* versions of that change. If you
> make this mistake, use `jj undo` to recover.

Once you're down to a single commit for that change ID, rerun your `jj-stack` command.
