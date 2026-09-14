---
title: Review and merge a stack on GitHub
linkTitle: Review a stack
description: Review each layer and configure GitHub checks and merges for native stacks.
navGroup: Everyday work
weight: 47
---

Each change appears as a pull request in GitHub's stack view. You can comment, approve, or
request changes without installing jj-stack.

## Read the stack map

GitHub shows the ordered stack and each pull request's position. Start at the bottom, where the
stack branches from its base branch, and move upward. Each pull request's **Files changed** view
shows the diff for that layer. Review and approve each layer independently, while reading higher
layers with their dependencies in mind. See GitHub's [guide to reviewing stacks][github-review].

From a checkout that tracks the stack, you can print the same order before opening GitHub:

```console
jj-stack view --pull-request <pr>
```

## Review revisions

After an author resubmits a changed PR, look for its **Revision history** comment. The newest
version appears first and is marked **current**. Use **Changes from previous version** to see
what changed in that update. Use **Submitted commit** to inspect the exact commit published for
that version.

The comment lists the most recent versions. If you missed several updates, follow their diff
links in order; use the PR's **Files changed** tab to review the current layer as a whole.

## Review and merge in order

Approvals, requested changes, and checks apply to each PR, so a lower layer can be approved while
work continues above it. Merge from the bottom upward: select the highest PR you want to merge
and use its stack merge controls. GitHub merges that PR and every unmerged PR below it. The PRs
above it remain open. See GitHub's [merging guide][github-merge] for the controls and
requirements.

After GitHub finishes, the author should run `jj-stack sync <head-change-id>` to update local
history and any remaining PRs.

## Configure rules and CI

Configure merge requirements on the stack's base branch, usually `main`. GitHub enforces those
requirements, including required reviews, CODEOWNER approvals, and checks, on every PR in the
stack, even when its immediate base is another PR branch. See GitHub's
[rules and CI guidance][github-rules].

GitHub Actions workflows for pull requests targeting that base also run for every layer. A
workflow configured for pull requests to `main`, for example, covers the whole stack. If that
multiplies an expensive CI workload, use stack metadata to choose where each job runs. GitHub's
[CI guide][github-ci] provides examples. Keep the checks required before merge meaningful for
each PR.

## Merge queues

GitHub adds a stack's pull requests to the queue in dependency order. Removing or ejecting a PR
also removes every PR above it. Resolve the cause, then add the stack to the queue again. Wait
until the merge completes before running `jj-stack sync`. See GitHub's
[merge queue guidance][github-queue].

[github-review]: https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/reviewing-stacked-pull-requests
[github-merge]: https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-stacked-pull-requests
[github-rules]: https://docs.github.com/en/pull-requests/get-started/about-stacked-prs#rules-and-ci-enforcement
[github-ci]: https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/optimizing-ci-for-stacked-pull-requests
[github-queue]: https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-stacked-pull-requests#merging-using-a-merge-queue
