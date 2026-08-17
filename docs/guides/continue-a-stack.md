---
title: Continue an existing stack
linkTitle: Continue an existing stack
description: Connect this checkout to your existing stack of pull requests on GitHub.
navGroup: Everyday work
weight: 70
---

Use this workflow when you submitted your stack from another machine or checkout and want to work
on it here.

## Pick a stack

List the active stacks already tracked here and those available only on GitHub:

```console
jj-stack checkout --pick
```

Each GitHub row shows the stack number, top pull request, base branch, size, status, and whether
the stack is local, partly local, or available only on GitHub. Choosing a partly local or
GitHub-only stack completes its local tracking, fetches any missing commits, and edits its top
active change. Choosing a local stack just edits its head.

## Connect a pull request directly

Choose any pull request in your stack:

```console
jj-stack checkout --pull-request <pr>
```

`checkout` follows the selected pull request down to the bottom of its stack, fetches those
commits, records which local change belongs to each pull request, and runs `jj edit` on the
selected pull request's change.

To start a new change on top instead of editing that change directly, run:

```console
jj new
```

## If your change is already here with local edits

`checkout` stops if this repo already contains a different commit for the same change. This
usually means that you edited it locally after it was last submitted. It will not choose between
the submitted snapshot and the local rewrite.

Connect your pull request to the local change you want to keep, then update your stack on GitHub:

```console
jj-stack relink <pr> <change-id>
jj-stack submit <head-change-id>
```

`relink` is a repair command for this specific mismatch. It verifies that your pull request's
branch belongs to your selected change; it cannot attach an unrelated pull request to new work.
