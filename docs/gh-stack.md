---
title: jj-stack and gh stack
linkTitle: Compare with gh stack
description: Choose the tool whose local model matches the repository you work in.
navGroup: Look things up
weight: 115
---

Both tools create native GitHub stacks: one pull request per reviewable step, based on the one
below it. They differ in what defines the stack locally.

| Topic | `jj-stack` | `gh stack` |
|---|---|---|
| What defines the order | The parent order of `jj` changes | An ordered list of Git branches |
| Review unit | One mutable `jj` change | One branch with one or more commits |
| Review branches | Remote-only and normally hidden | Ordinary local and remote branches |
| Restructure | Standard `jj` history editing | `gh stack` commands and its modify UI |
| Refresh reviews | Submit after a `jj` rewrite | Rebase higher branches, then push or submit |
| Continue work elsewhere | Adopt PRs and edit the selected change | Create branches and switch |

## Which should I use?

- In a `jj` repository, use `jj-stack` and author the stack as mutable changes.
- In a Git repository, `gh stack` manages the stack as an explicit branch chain.

`gh stack link` is useful when another tool already manages one stable branch per pull request and
you only need to tell GitHub that the pull requests form a stack. In a `jj` workflow, jj-stack
also remembers which change belongs to each pull request.

Do not use both tools to update the same review branches. They organize local work differently,
and jj-stack will stop if another tool moves one of its branches unexpectedly.

The GitHub review and merge experience is the same. After merging on GitHub or in another client,
update the local `jj` stack with:

```console
jj-stack sync <head-change-id>
```
