---
title: Continue an existing stack
linkTitle: Continue an existing stack
description: Connect this checkout to your existing stack of pull requests on GitHub.
navGroup: Everyday work
weight: 70
---

Use `jj-stack checkout` to continue work submitted with jj-stack from another machine or
checkout. It fetches the changes you need, saves their pull request links, and switches your
working copy to the selected change.

The PRs and their head branches must belong to the repo selected by your
[Git remote](../reference/configuration.md#git-remote). Each PR branch must use jj-stack's branch
naming scheme with the prefix configured in this checkout, normally `jj-stack/`. If the original
checkout used a custom prefix, set the same
[`jj-stack.branch_prefix`](../reference/configuration.md#pr-branch-names) here before checking
out the stack. PRs with head branches in another repository, such as a contributor's fork, are
not supported.

## Pick a stack

Choose from local stacks and stacks on GitHub:

```console
jj-stack checkout --pick
```

The picker shows each GitHub stack's top PR, base branch, size, and status, along with whether it
is already available locally. Choose a stack to fetch any missing commits, save its pull request
links, and run `jj edit` on its top unmerged change. For a stack already tracked here, the command
edits its local head change.

If this checkout already tracks the stack, you can skip the picker and edit its head directly:

```console
jj-stack checkout --revset <head-change-id>
```

This works like `jj edit`, but first confirms that every change in the stack has a saved pull
request link. It does not contact GitHub.

## Check out a specific pull request

If you know the PR number or URL, select it directly:

```console
jj-stack checkout --pull-request <pr>
```

`jj-stack checkout` brings in that PR and the PRs below it, then runs `jj edit` on the selected
PR's head commit. Select the top PR to check out the whole stack; selecting a middle PR does not
bring in the changes above it.

To start a new change on top instead of editing that change directly, run:

```console
jj new
```

## Resolve different versions of the same change

If you edited a change locally after submitting it, `jj-stack checkout` may bring in the PR's
version alongside your local version. It prints their commit IDs so you can compare them:

```console
jj log -r 'change_id(<change-id>)'
jj diff -r <first-commit-id>
jj diff -r <second-commit-id>
```

Keep any edits you need, then abandon the unwanted version by its **commit ID**. Both versions
share a change ID, so a bare change ID is ambiguous:

```console
jj abandon <unwanted-commit-id>
```

If you kept or combined local edits, update the pull request:

```console
jj-stack submit <head-change-id>
```

## If someone pushed a commit to your PR branch

If someone added a commit to your PR branch, such as a reviewer's suggestion, `jj-stack checkout`
brings it in as a new change on top of yours. To include it in the existing PR, run the
`jj squash` command printed by checkout, then run `jj-stack submit <head-change-id>`.
