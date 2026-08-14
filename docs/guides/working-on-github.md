---
title: Work with a stack on GitHub
linkTitle: Work on GitHub
description: Know which GitHub edits jj-stack preserves, replaces, or rejects.
navGroup: Everyday work
weight: 45
---

GitHub remains the place to review, discuss, check, and merge a stack. Your local `jj` history
remains the source of truth for its changes and order.

## Safe GitHub actions

You can comment, review, approve, request changes, add labels, request reviewers, and inspect or
rerun checks normally. You can also merge through GitHub's native stack UI or use **Rebase
stack**. After a merge or rebase finishes, run:

```console
jj-stack sync <head-change-id>
```

Do not rewrite the same stack locally while a GitHub rebase or merge is in progress. If local
edits and GitHub's rewritten contents disagree, `sync` stops instead of choosing one.

## What the next submit replaces

`jj-stack submit` makes GitHub match the selected local stack. It pushes each selected change to
its review branch, sets each pull request's base from the local parent order, refreshes titles and
bodies from `jj` descriptions or supplied description files, and updates native stack membership.
`--draft` affects new pull requests. `--draft=all`, `--open`, and the choices made through
`--edit` can change existing draft states. Labels and reviewer requests that submit applies are
additive;
unrelated existing labels and reviewers are not removed.

Edits made directly to a pull request title or body are therefore temporary unless you copy them
back into the change description or pass them again on the next submit.

## Changes to avoid on GitHub

Do not force-push, rename, or delete `jj-stack/` review branches. Do not manually retarget pull
request bases, reorder members, or add pull requests to the native stack when you intend the
local stack order to remain authoritative. A later submit may replace grouping and bases; an
unexpected branch move instead causes jj-stack to stop so it does not overwrite someone else's
work.

If an external edit was intentional, inspect the result before deciding whether to restore the
GitHub state, reproduce the change with `jj` and submit it, or remove the GitHub grouping with
`jj-stack unstack --stack <number>`. See [troubleshooting](../troubleshooting.md) for specific
recovery paths.

For reviewer and repository configuration guidance, see
[review and operate a stack](review-a-stack.md).
