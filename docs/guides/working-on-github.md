---
title: Work with a stack on GitHub
linkTitle: Work on GitHub
description: Know which GitHub edits jj-stack preserves, replaces, or rejects.
navGroup: Everyday work
weight: 45
---

Use GitHub to review, discuss, run checks, and merge. Use `jj` to edit the changes and their
order, then `jj-stack submit` to publish the result.

## Review, merge, and rebase on GitHub

You can comment, review, approve, request changes, add labels, request reviewers, and inspect or
rerun checks normally. You can also merge through GitHub's native stack UI or use **Rebase
stack**. After a merge or rebase finishes, run:

```console
jj-stack sync <head-change-id>
```

Wait for a GitHub rebase or merge to finish before rewriting the same stack locally. After a
GitHub rebase, `jj-stack sync` checks that rebasing your local changes produces the same contents
as GitHub's version. If they disagree, it stops and leaves the PR branches untouched. See
[merge and sync](merge-and-sync.md) for the full workflow.

## Titles, bodies, drafts, and labels

You can edit pull request titles and bodies on GitHub. On the next submit, jj-stack checks both
against the text generated from the last submitted change. If both still match, it refreshes
them from your local description. If either differs, it preserves both.

Use `--describe` to replace one body deliberately (or `--describe-with` or `--edit` for titles and
bodies). See [pull request descriptions](../reference/descriptions.md) for examples.

Existing PRs keep their draft state unless you submit with `--draft=all`, `--open`, or an edited
draft choice from `--edit`. Plain `--draft` affects only new PRs.

Submitting can add labels and request reviewers, but it does not remove existing labels or
reviewer requests. Set defaults or make explicit requests as described in
[submit and update](submit-and-update.md#reviewers-and-labels).

## Changes to avoid on GitHub

Let jj-stack manage its PR branches, whose names normally start with `jj-stack/`. Do not
force-push, rename, or delete them. An unexpected branch change stops submission; see
[troubleshooting](../troubleshooting.md) for recovery steps. GitHub's **Rebase stack** operation
is supported through `jj-stack sync`, as described above.

Do not change pull request bases or GitHub stack membership by hand. `jj-stack submit` derives
both from the local `jj` history. To change the base or order, make that change locally and
submit again. To remove the GitHub stack while leaving the PRs open, see
[separate a stack](close-or-separate.md#remove-a-github-stack).

Do not enable auto-merge, shown as **Merge when ready** with a merge queue, on a pull request
you will stack more changes on. GitHub refuses to add such a PR to a stack, so the next submit
stops until you disable it.

For reviewer and repo configuration guidance, see
[review and merge a stack on GitHub](review-a-stack.md).
