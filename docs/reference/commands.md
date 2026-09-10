---
title: Command reference
linkTitle: Commands
description: Find the right command for a task and open its complete built-in help.
navGroup: Look things up
weight: 80
---

Use built-in help to look up flags, aliases, and examples:

```console
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

`jj-stack help --all` includes advanced commands and global options. The
[complete CLI reference](https://www.serpentine.com/software/jj-stack/reference/cli/) combines
the help for every command on one page.

## Core workflow

| Command | Use it to |
|---|---|
| `view` | Show a local stack and the status of its pull requests. |
| `list` | List tracked stacks and orphaned PRs in this repo. |
| `submit` | Create or update PR branches and pull requests. |
| `merge` | Ask GitHub to merge ready pull requests from the bottom of a stack. |
| `sync` | Update local history after GitHub merges or rebases, then refresh remaining PRs. |

Running `jj-stack` without a subcommand is equivalent to `jj-stack view` without arguments.

## Connect or repair pull requests

| Command | Use it to |
|---|---|
| `checkout` | Fetch an existing PR stack, link it to local changes, and edit the chosen change. |
| `relink` | Resume updating PRs after `unstack --local` or a push from another checkout. |
| `doctor` | Check repo setup, GitHub access, and leftovers from interrupted commands. |
| `in-use` | Silently check whether this repo has jj-stack tracking data. |

## Separating stacks or closing pull requests

| Command | Use it to |
|---|---|
| `unstack` | Tell GitHub that a set of open pull requests is no longer one stack. |
| `cleanup` | Remove unused PR branches, managed comments, and saved pull request links. |

## Supporting tools

| Command | Use it to |
|---|---|
| `completion` | Generate shell completion for `jj-stack` or a `jj stack` alias. |
| `help --all` | Include advanced commands and global options in top-level help. |

## Choose a stack

Pass a head change ID to `view`, `submit`, `merge`, or `sync` to select a stack explicitly. Where
supported, use `--pull-request` to select by PR number or URL.

After a failed or interrupted command, use the head change ID printed by `jj-stack view` when you
retry. It still identifies the change if the working copy moves or you rewrite the stack.

See [Bookmarks and stack selection](bookmarks-and-selection.md) for how bookmark names, change
IDs, and other revision expressions choose a stack.
