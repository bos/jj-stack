---
title: Close or separate pull requests
linkTitle: Close or separate PRs
description: >-
  Break up your GitHub stack without closing your pull requests, or close your work without
  merging it.
navGroup: Everyday work
weight: 75
---

Use this page when you no longer want to merge your stack in its current form. What you do next
depends on whether your pull requests are still useful:

- To keep them open but stop treating them as one GitHub stack, use `unstack`.
- To abandon your work, close your pull requests and then use `cleanup` to remove what jj-stack
  no longer needs.

## Keep your pull requests open but separate them

Use this when your pull requests are still useful but no longer need to depend on one another or
merge in a particular order. For example, after revising your work, you may decide that each pull
request can be reviewed and merged on its own.

Remove your GitHub stack grouping:

```console
jj-stack unstack <head-change-id>
```

Your pull requests remain open. You can review, update, or close them individually afterward.

If jj-stack says the GitHub grouping no longer matches your local stack, rerun `unstack` with the
`--stack <number>` command printed in the error.

## Close your stack without merging it

Use this when you have decided not to land your changes—for example, because the work is no
longer needed or another approach replaced it. Closing your pull requests records that decision
on GitHub; cleanup removes the branches, comments, and saved links that jj-stack no longer needs.

If your pull requests are currently grouped as a GitHub stack, run `unstack` as shown above. Then
close each of your pull requests on GitHub or with `gh`:

```console
gh pr close <pr>
```

Finally, remove your unused review branches, stack overview comments, and saved pull-request
links:

```console
jj-stack cleanup <head-change-id>
```

Cleanup leaves a review branch in place if an open pull request still needs it. Close or retarget
that pull request, then run the same cleanup command again.

## Close an orphaned pull request

After you abandon one of your changes with `jj abandon`, `jj-stack list` shows its pull request
as an orphan. Because you no longer have the local change, select the orphaned pull request
directly:

```console
jj-stack cleanup --pull-request <pr> --close
```

This closes your pull request if it is open, then removes its unused branch, stack overview
comment, and saved link.
