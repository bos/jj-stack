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

**How this can happen:** you just cloned your repo, its Git remote changed, or your GitHub
login expired.

Prepare your repo again and look for any failed checks:

```console
jj-stack doctor --fix
```

`doctor` checks your repo, trunk, Git remote, GitHub access, and Stacks API availability.
It does not change anything on GitHub.

### The repo works, but GitHub stacks are unavailable

If `doctor` can reach the repo but says stacked pull requests are unavailable, follow its
availability link and rerun `doctor`. For another `GitHub stacks` failure, follow the specific
access or request error it prints. `submit` stops before pushing any PR branch while this
check fails.

## A PR branch moved outside jj-stack

**How this can happen:** someone force-pushed, renamed, or deleted a `jj-stack/` branch, or
another tool updated it.

`jj-stack` leaves the branch untouched and prints a recovery hint for the condition it observed.
For an accidentally moved `submit --base` branch, restore the immutable submitted commit ID named
in that error. For another moved branch, inspect and repair it as the hint directs; for a missing
branch, either restore it or close the PR on GitHub, run `jj-stack cleanup`, and submit again.

If remote contents are intentional, preserve or reproduce them in the local `jj` change before
submitting. Use `jj-stack relink <pull-request> <change-id>` only when the PR is open and unique,
its branch belongs to the same repo and remains a managed `jj-stack/` name for that change,
and its head still carries that jj change ID. `relink` verifies those conditions and updates
tracking; it does not copy the remote contents into your local change.

Do not force a submit past the mismatch. The stop is what prevents one tool from silently
overwriting another tool's work.

## A pull request was added to or reordered in the GitHub stack

**How this can happen:** someone changed native stack membership or pull request bases through
GitHub or another client.

Run `jj-stack view <head-change-id>` and compare the GitHub order with your local stack. If the
GitHub edit is the intended order, reproduce it with `jj` and submit the resulting local stack.
If the local order is intended, submit it; jj-stack replaces unambiguous native grouping and pull
request bases. When the diagnostic says membership is ambiguous, remove the named GitHub grouping
with `jj-stack unstack --stack <number>`, then submit the intended local stack again.

## A stack was removed from the merge queue

GitHub enqueues stack members in dependency order. If one pull request is removed or ejected,
GitHub also removes every pull request above it. Fix the failing check, approval, conflict, or
repo rule, then rerun the same `jj-stack merge` command. If GitHub merged any lower pull
requests before the ejection, run `jj-stack sync <head-change-id>` first.

## You merged pull requests on GitHub

**How this can happen:** you clicked Merge on GitHub, someone else merged your pull requests, or
a merge queue finished while you were away.

Run `sync` to apply the merge locally, update your remaining pull requests, and remove PR
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

GitHub's rewritten commits do not contain jj change-ID headers. This is expected; do not relink
the pull requests by hand. `sync` verifies the rewritten contents, rebases the original local
changes, and updates the PR branches with equivalent commits that retain their change IDs. If
you changed the stack locally after submitting it, `sync` stops rather than choosing between your
local work and GitHub's result.

## `merge` did not merge your whole stack

**How this can happen:** `merge` works upward from the bottom of your stack. It stops when it
reaches a pull request that is a draft, no longer matches your local change, or cannot be merged
by GitHub.

The output identifies which of your pull requests it left open and explains why:

- If your local changes no longer match what you submitted, run
  `jj-stack submit <head-change-id>`, then retry `merge`.
- If GitHub reports a pending check, missing approval, draft pull request, repo rule, or
  permissions problem, fix that on GitHub, then retry the same `merge` command.
- If GitHub reports a conflict, rebase and resolve it with `jj`, submit the updated stack, then
  retry `merge`.
- If that pull request was already merged separately, run `jj-stack sync <head-change-id>`.

If all that happened was that trunk advanced, you may not need to rebase. GitHub can merge your
stack while its base is behind trunk when it has no conflicts.

## GitHub merged your stack, but `merge` ended with an error

**How this can happen:** GitHub completed the merge, then your network connection failed or
`jj-stack` could not finish updating your local repo.

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

## Your old PR branches remain

**How this can happen:** your pull requests were merged or closed outside `jj-stack`, or cleanup
was interrupted.

If your pull requests were merged, run `sync` first. If they are closed and no longer needed, run
cleanup:

```console
jj-stack cleanup <head-change-id>
```

Cleanup leaves a PR branch in place while an open pull request still needs it.

## “The selector resolved to more than one commit”

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
