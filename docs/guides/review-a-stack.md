---
title: Review and operate a stack
linkTitle: Review a stack
description: Review each layer and configure GitHub checks and merges for native stacks.
navGroup: Everyday work
weight: 47
---

This page is for reviewers and repo administrators. Authors can keep using ordinary `jj`
locally; reviewers work with native stacked pull requests on GitHub.

## Read the stack map

GitHub shows the ordered stack and each pull request's position. Start at the bottom, which is
closest to the final base branch, and move upward. Each pull request's **Files changed** view is
the diff for that layer, not the cumulative diff from the final base. Review and approve each
layer independently, while reading a higher layer with its dependencies in mind.

## Review and merge in order

Comments, requested changes, approvals, CODEOWNERS, and required checks remain per pull request.
A lower layer can be approved while work continues above it. When merging only part of a stack,
merge a contiguous section from the bottom; the remaining pull requests still depend on the
merged work and the author should run `jj-stack sync` afterward.

Use GitHub's native stack merge controls for merges started in the web UI. The ordinary legacy
pull request merge API does not implement the same stack operation. For current UI behavior and
limitations, see GitHub's [stacked pull request guides][github-stacks] for the current controls.

## Configure rules and CI for the final base

GitHub evaluates every layer against the stack's final base branch, even though an individual
pull request directly targets the layer below it. Required reviews, CODEOWNERS, rulesets, and
required status checks on that final base therefore apply to every pull request in the stack.
GitHub Actions workflows triggered for pull requests to the final base also run for every layer.

Keep required checks meaningful for each layer. If running the full suite for every pull request
is too expensive, use GitHub's stack metadata to choose cheaper layer checks without weakening
the checks required before merge. GitHub's [stacked pull request guides][github-stacks] link to
the current rollout and CI articles about events and stack metadata.

## Merge queues

GitHub enqueues a stack's pull requests in dependency order. If a pull request is removed or
ejected, GitHub also removes every pull request above it. Resolve the failing rule or check, then
enqueue the stack again. Queue acceptance is not the same as a completed merge. After GitHub
reports completion, the author runs `jj-stack sync <head-change-id>` to update local history and
the remaining PRs.

Repo behavior and preview limitations can change independently of jj-stack. Use GitHub's
[stacked pull request guides][github-stacks] for the current platform rules instead of copying
those details into local team instructions.

[github-stacks]: https://docs.github.com/pull-requests/how-tos/stacked-pull-requests
