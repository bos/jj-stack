---
title: Edit and rearrange a stack
linkTitle: Edit and rearrange
description: >-
  Change your submitted stack with ordinary jj commands while keeping the same pull requests.
navGroup: Everyday work
weight: 40
---

Use `jj` for your local history edits. Use `jj-stack submit` afterwards to make GitHub match.

## Edit a change in your stack

Edit the change you want, then resubmit your stack:

```console
jj edit <change-id>
# make the requested changes
jj-stack submit <head-change-id>
```

`jj-stack` updates your existing pull request, preserving its discussion.

## Reorder changes

Rebase or rearrange your work with `jj`, optionally inspect the result, then submit:

```console
jj arrange
jj-stack view <head-change-id>
jj-stack submit <head-change-id>
```

`submit` updates your pull request order to match.

## Split or squash

When you split one of your changes, the part that keeps the original change ID also keeps its
pull request. Your new change gets a new pull request.

When you squash your changes, whichever change survives keeps its pull request. Pull requests for
changes that are no longer in your stack remain open until you close and clean them up; jj-stack
never reuses one for different work.

## Abandon one of your submitted changes

Using `jj abandon` removes your change from your local history, not from GitHub. Your orphaned
pull request and PR branch remain until you close your pull request and run cleanup:

```console
jj-stack cleanup --pull-request <pr> --close
```

For a whole stack, follow [Separate a stack or close pull requests](close-or-separate.md).
