---
title: Command reference
linkTitle: Commands
description: Find the right command for a task and open its complete built-in help.
navGroup: Look things up
weight: 80
---

The built-in help has the complete list of flags and aliases:

```console
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

This page explains which command owns a task without duplicating every flag.

## Core workflow

| Command | Use it to |
|---|---|
| `view` | Show local stacks and the status of their pull requests. |
| `list` | Show every stack jj-stack knows about in this repository. |
| `submit` | Create or update review branches and pull requests. |
| `merge` | Ask GitHub to merge ready pull requests from the bottom of a stack. |
| `sync` | Apply a completed GitHub merge or stack rebase locally, update remaining pull requests, and remove review branches that are no longer needed. |

Running `jj-stack` without a subcommand is equivalent to `jj-stack view` without arguments.

## Connect or repair reviews

| Command | Use it to |
|---|---|
| `checkout` | Connect existing pull requests to local changes and edit the selected change. |
| `relink` | Tell jj-stack which change an existing pull request belongs to. |
| `doctor` | Check repository setup, GitHub access, and leftovers from interrupted commands. |
| `in-use` | Silently check whether jj-stack is set up in this repository. |

## Ending reviews

| Command | Use it to |
|---|---|
| `unstack` | Tell GitHub that a set of open pull requests is no longer one stack. |
| `cleanup` | Remove unused review branches, comments, and local pull-request links. |

## Supporting tools

| Command | Use it to |
|---|---|
| `completion` | Generate shell completion for `jj-stack` or a `jj stack` alias. |
| `help --all` | Print every command, option, and alias in one document. |

## Choose a stack

`view`, `submit`, `merge`, and `sync` accept a change ID when the working copy does not select the
intended stack. Commands that accept pull request selection spell it explicitly with
`--pull-request`.

After a failed or interrupted command, use the head change ID printed by `view`. It still selects
the same stack if the working copy later moves.
