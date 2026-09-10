---
title: Troubleshooting
description: Recover from failed commands, changed PR branches, and conflicting local versions.
navGroup: Fix a problem
weight: 120
---

Start with the `Hint:` at the end of the error. It usually names a command for the problem
jj-stack found.

The examples below use `<head-change-id>` to identify your stack. If you do not know that ID, run
`jj-stack list` and copy your stack's head change ID.

## Setup or GitHub access fails

Run the setup checks after cloning a repo, changing its Git remote, or encountering an
authentication error:

```console
jj-stack doctor --fix
```

`jj-stack doctor` checks your repo, Git remote, GitHub access and push permission, trunk, and
that GitHub's Stacks feature is available for your remote. With `--fix`, it also fixes up your
fetch config to hide GitHub book-keeping branches, and removes leftovers from interrupted
commands. Follow the guidance for any checks that still fail. It does not change GitHub.

## The pull requests in a stack are not numbered in ascending order

This is expected. I didn't consider preserving ascending numeric order to be worth the cost in
perceived performance.

GitHub's API is very slow, costing many hundreds of milliseconds per round-trip, with high
variability. To be as fast as possible, `jj-stack` creates and updates PRs in a stack
concurrently. This means that e.g. PR 42 may be created with PR 43 as its parent, because GitHub
assigned 43 a number a moment earlier than 42.

I chose this in part because I don't personally care about the numeric ordering, but there's a
logic to it too. You can move changes around or create new ones inside a stack any time, which
will naturally change the order of the PRs when you `submit` again. So "a higher-numbered PR is
always a descendant of a lower-numbered PR" was at best an occasional rule of thumb rather than
something that could be depended on.

## You want to use the same PRs again after `unstack --local`

`jj-stack unstack --local` removes the local links between your changes and their PRs. It leaves
the PRs open on GitHub. If you later want `jj-stack submit` to update those PRs again, restore
the links with `jj-stack relink`.

Find the PR numbers on GitHub and the matching change IDs with `jj log`. For each PR, run:

```console
jj-stack relink <pr> <change-id>
```

Once you have relinked every PR in the stack, submit from its top change:

```console
jj-stack submit <head-change-id>
```

`jj-stack relink` only restores the local link. The later submit updates the existing PRs,
keeping their numbers and discussions. You can use the same steps if you deleted jj-stack's
local tracking file.

If `jj-stack relink` reports that the local and GitHub versions differ, [choose which work to
keep](#a-pr-branch-moved-outside-jj-stack) before retrying.

## A PR branch moved outside jj-stack

If someone updates your PR branch from another checkout, your local change may not include
their work. `jj-stack submit` stops so it does not overwrite that version on GitHub.

Inspect the PR on GitHub, then decide what to do with the work there:

- To keep the work, run `jj-stack checkout --pull-request <pr>`. It brings the PR's commits into
  your repo. [Compare and combine the versions](guides/continue-a-stack.md), then run
  `jj-stack submit <head-change-id>`.
- To replace it with your local version, run
  `jj-stack relink --replace-remote <pr> <change-id>`, then `jj-stack submit <head-change-id>`.
  Here, `jj-stack relink --replace-remote` allows the next submit to overwrite the version on
  GitHub. The submit then pushes your local change to the existing PR.

If GitHub changed the branch while merging or rebasing your stack, run
`jj-stack sync <head-change-id>` instead. It brings GitHub's completed merge or rebase into your
local stack.

`jj-stack relink` reconnects a PR to its original change. Even with `--replace-remote`, it
cannot transfer the PR to a different change ID.

If you replaced the original change with a new one, run `jj-stack checkout --pull-request <pr>`
to recover the original change, then move the edits you want to keep onto it.

If the error names a `--base` parent's branch, restore that branch to the commit ID in the error
before retrying the child submission. Submitting a child stack does not update its parent.

For a missing branch, either restore it or [close and clean up the old PR](
guides/close-or-separate.md#close-the-prs-in-your-stack-without-merging-them), then submit again
to create a new PR.

## A pull request was added to or reordered in the GitHub stack

The local `jj` history determines PR order. Compare it with the stack on GitHub. If you want the
GitHub order, reproduce it locally with `jj`, then submit. If you want the local order, run
`jj-stack submit <head-change-id>` to update the PR bases and GitHub stack.

If the error says the PRs do not identify one GitHub stack, remove the GitHub stack named in the
error with `jj-stack unstack --stack <number>`, then submit again. This keeps the PRs open.

## A stack was removed from the merge queue

If a pull request is removed or ejected from the merge queue, [GitHub also removes the PRs above
it][stack-merges]. Check the reason on GitHub and fix the failing check, missing approval,
conflict, or repo rule. If GitHub merged any lower PRs, run `jj-stack sync <head-change-id>`.
Then rerun the same `jj-stack merge` command.

[stack-merges]: https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-stacked-pull-requests

## You merged pull requests on GitHub

After a merge through GitHub or another client, or after a merge queue finishes, run
`jj-stack sync` to update your local stack, refresh the remaining PRs, and remove unused PR
branches:

```console
jj-stack sync <head-change-id>
```

If GitHub also rewrote the remaining PR branches, `jj-stack sync` handles those rewrites and
keeps any local edits to the remaining changes.

To sync every stack affected by a completed merge, run:

```console
jj-stack sync --all
```

A blocked stack does not prevent jj-stack from syncing independent stacks. If a selected PR is
still in a merge queue, sync leaves that stack unchanged; wait for GitHub to finish.

## You rebased your stack on GitHub

After GitHub's **Rebase stack** action completes, run:

```console
jj-stack sync <head-change-id>
```

`jj-stack sync` rebases your original local changes and updates the PR branches with equivalent
commits that retain their jj change IDs. There is no need to relink the PRs. It stops if local
edits or different contents on GitHub prevent it from matching the two versions.

Select this stack explicitly: `jj-stack sync --all` handles completed merges, not GitHub stack
rebases.

## `merge` did not merge your whole stack

**How this can happen:** `merge` selects consecutive open, non-draft PRs from the bottom of your
stack that still match what you submitted. A draft or a changed local commit can limit that
selection. GitHub then accepts or rejects the selected group as a whole. If a check or approval
blocks it, `jj-stack` does not automatically retry with a smaller group.

Use the reason in the output to choose the next step:

- If your local changes no longer match what you submitted, run
  `jj-stack submit <head-change-id>`, then retry `jj-stack merge`.
- If GitHub reports a pending check, missing approval, draft pull request, repo rule, or
  permissions problem, fix that on GitHub, then retry the same `jj-stack merge` command.
- If GitHub reports a conflict, rebase and resolve it with `jj`, submit the updated stack, then
  retry `jj-stack merge`.
- If that pull request was already merged separately, run `jj-stack sync <head-change-id>`.

To land a smaller group whose checks and approvals are ready, use
`jj-stack merge --pull-request <last-pr-to-merge>`.

If all that happened was that trunk advanced, you may not need to rebase. GitHub can merge your
stack while its base is behind trunk when it has no conflicts.

## GitHub merged your stack, but `merge` ended with an error

The merge completed, but jj-stack could not finish updating your local stack or cleaning up
GitHub. This can happen after a network failure or an interrupted local update. If the local
rebase produced conflicts, follow
[sync conflict recovery](#sync-rebased-your-changes-into-conflicts). Otherwise, follow the
recovery instructions in the error.

Do not retry the merge; the PRs are already merged.

## `sync` rebased your changes into conflicts

If a rebase produces conflicts, `jj-stack sync` keeps the local rebase but stops before updating
the remaining PRs or cleaning up merged PRs. This can also happen during the automatic sync at
the end of `jj-stack merge`.

Resolve the conflicts with `jj`, then run the `jj-stack submit` command printed in the hint:

```console
jj-stack submit <head-change-id>
```

Use `submit` after resolving these conflicts: the local rebase is already applied, so rerunning
`sync` may find no merged changes left to process.

If unused branches or saved links remain for merged PRs, clean up each by its PR number:

```console
jj-stack cleanup --pull-request <merged-pr>
```

## A command was interrupted

After Ctrl-C, lost connectivity, or a terminal closing mid-command, inspect the current state:

```console
jj-stack view <head-change-id>
```

If `jj-stack submit --edit` fails after you edit the pull requests, you can retry without typing
your edits again. jj-stack keeps the file you edited and prints its location after `Editor file:`
or in the error's retry command. Use that location in this command:

```console
jj-stack submit <head-change-id> --resume-edit /path/to/saved-editor-file.md
```

`--resume-edit` opens your saved edits in the editor again. Use it instead of `--edit`, which
opens a new file. Include any other options from your original command, such as `--base`.
See [edit every PR at once](reference/descriptions.md#edit-every-pr-at-once) for more details.

For other interruptions, if GitHub completed a merge, run `jj-stack sync <head-change-id>`.
Otherwise, rerun your original command. jj-stack checks what already succeeded and continues
from the current state.

## Your old PR branches remain

For merged PRs, run `jj-stack sync <head-change-id>` first. For closed PRs, or to retry unfinished
cleanup, run:

```console
jj-stack cleanup <head-change-id>
```

Cleanup keeps a branch while another open or reopenable closed PR uses it as a base, or an
unmerged PR in a GitHub stack needs it. See [what to do when cleanup keeps a branch](
guides/close-or-separate.md#if-cleanup-keeps-a-branch).

## “The selector resolved to more than one commit”

Your revset matched several commits, but the command needs one stack head.

Run `jj-stack list` to identify your intended stack, then rerun your failing command with that
stack's head change ID.

## “Divergent changes are not supported”

Two or more local commits share a change ID. This can happen when separate workspaces modify a
change independently, or when `jj-stack checkout --pull-request` brings in a PR's version of a
change you also edited locally. jj-stack cannot choose which version belongs in your stack.

Show the versions and compare their diffs:

```console
jj log -r 'change_id(<change-id>)'
jj diff -r <first-commit-id>
jj diff -r <second-commit-id>
```

Keep or combine the edits you need, then abandon the unwanted version by its **commit ID**.
The versions share a change ID, so a bare change ID is ambiguous:

```console
jj abandon <unwanted-commit-id>
```

Once you're down to a single commit for that change ID, rerun your `jj-stack` command.
