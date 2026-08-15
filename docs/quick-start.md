---
title: Quick start
linkTitle: Quick start
description: Install jj-stack and submit your first stack in a few minutes.
navGroup: Start here
weight: 10
---

## Requirements

- Python 3.14 or newer
- [`uv`](https://docs.astral.sh/uv/)
- `jj` 0.44.0 or newer
- a GitHub repository you can push to, and GitHub authentication

GitHub stacked pull requests are in
[public preview](https://docs.github.com/en/pull-requests/tutorials/roll-out-stacked-prs) and
require no repository or organization setup.

## Install

Install `jj-stack` from PyPI:

```console
uv tool install jj-stack
```

To upgrade later, rerun the command with `--force`.

## Prepare the repository

Inside your `jj` repository, prepare it for use:

```console
jj-stack doctor --fix
```

Confirm that the `GitHub stacks` check passes. `doctor` explains how to resolve an unavailable
Stacks API before `submit` pushes anything.

## Build your local stack

Create a linear series of `jj` changes above `trunk()`. For example:

```console
jj new -m "refactor shared model"
# edit files
jj new -m "add API"
# edit files
jj new -m "add UI"
# edit files
```

Keep using ordinary `jj` commands to create and rearrange your local changes. `jj-stack` will take
care of the GitHub review side.

## Inspect and submit

Run `jj-stack` with no subcommand to see your stack:

```console
jj-stack
```

Submit your stack for review:

```console
jj-stack submit
```

`submit` creates one pull request for each of your changes, links your pull requests in the same
order, and creates your stack on GitHub.

## Revise normally

Edit, split, squash, or reorder your changes with `jj`. When your changes are ready for another
round of review, update your existing pull requests:

```console
jj-stack submit
```

Because a `jj` change keeps its change ID when you edit it, `jj-stack` updates your existing pull
request instead of opening a new one.

## What next?

- Read [how jj-stack works](mental-model.md) before restructuring your submitted stack.
- Follow [submit and update](guides/submit-and-update.md) for descriptions, drafts, and reviewer
  requests.
- Read [merge and sync](guides/merge-and-sync.md) before landing your stack.
