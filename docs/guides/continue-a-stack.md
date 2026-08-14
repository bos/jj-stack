---
title: Continue an existing stack
linkTitle: Continue an existing stack
description: Connect this checkout to your existing stack of pull requests on GitHub.
navGroup: Everyday work
weight: 70
---

Use this workflow when you submitted your stack from another machine or checkout and want to work
on it here.

## Connect your pull requests to local changes

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

## What `--pick` does

`jj-stack checkout --pick` is only a convenience for selecting a stack this repository already
tracks. It lists the head change ID and subject of each tracked stack and asks you to choose a
number. The command then edits that head, just as if you had passed its change ID to `--revset`.

`--pick` does not discover stacks that exist only on GitHub. Use `--pull-request <pr>` to fetch
one of those stacks into this repository and edit the selected PR's change.

## If your change is already here with local edits

`checkout` stops if this repository already contains a different commit for the same change. This
usually means that you edited it locally after it was last submitted. It will not choose between
the reviewed snapshot and the local rewrite.

Connect your pull request to the local change you want to keep, then update your stack on GitHub:

```console
jj-stack relink <pr> <change-id>
jj-stack submit <head-change-id>
```

`relink` is a repair command for this specific mismatch. It verifies that your pull request's
branch belongs to your selected change; it cannot attach an unrelated pull request to new work.
