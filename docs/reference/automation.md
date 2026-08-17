---
title: Automation and agents
linkTitle: Automation and agents
description: >-
  Set up coding agents, inspect stacks from scripts, and handle command outcomes safely.
navGroup: Look things up
weight: 110
---

## Install the coding-agent skill

The bundled skill teaches coding agents how to use `jj-stack` without bypassing it with GitHub
or Git branch commands:

```console
gh skill install bos/jj-stack jj-stack
```

Installing the skill makes it available to the agent, but an agent might not know when to use
the skill. To have an agent use it automatically in repos that already use jj-stack, add this to
your personal agent instructions or the repo's agent instructions:

```markdown
## jj-stack

Before any GitHub pull request or branch task in a jj repo, run `jj-stack in-use`. If it
exits 0, load and follow the jj-stack skill. If it exits 1, continue without that skill. For any
other exit, stop and report the error. Cache the result for the repo. Check when the task
arises, not at session startup.
```

`in-use` is a silent, read-only check for `jj-stack`'s local tracking data. Exit 0 means the
repo uses `jj-stack`, exit 1 means it does not, and exit 11 means the check itself failed.

## Inspect stacks from a script

Use `view` when the script is concerned with one stack and `list` when it needs an inventory of
every tracked stack:

```console
jj-stack view --json <head-change-id>
jj-stack list --json
```

Both commands write JSON to standard output and diagnostics to standard error. Their output
follows `jj-stack`'s [published JSON
Schema](https://github.com/bos/jj-stack/blob/main/docs/json-output.schema.json).

In `view` output, the `stacks` array contains the selected stacks. In `list` output, the `rows`
array contains both stacks and orphaned PRs. A stack row contains a `changes` array ordered
from the bottom of the stack to its head.

Each change has a stable `status` value such as `unsubmitted`, `open`, `draft`, `approved`,
`changes_requested`, `merged`, or `closed`. Inspect those values rather than the stack row's
human-readable `status` summary.

### Keep partial reports

`view` and `list` exit 10 when they can report *some* state but could not inspect everything. With
`--json`, standard output still contains a valid payload. A script should keep and inspect it
while also recording that the report was incomplete.

For example:

```sh
report=$(mktemp)
if jj-stack list --json >"$report"; then
  result=complete
else
  code=$?
  if [ "$code" -eq 10 ]; then
    result=incomplete
  else
    exit "$code"
  fi
fi

# Parse "$report" here. "$result" says whether every lookup succeeded.
```

Exit 0 means inspection completed, not that every stack is ready to merge. The JSON may still
contain closed pull requests, orphaned PRs, or other work that needs attention.

## Select the same stack reliably

In scripts and coding agents, always discover and use a stack's head change ID explicitly. Do
not depend on whichever change happens to be the working copy when the script runs, because this
can change.

The head is the last entry in a stack's `changes` array. Change IDs remain stable when commits are
rewritten, which makes them suitable for passing from inspection to a later command:

```console
jj-stack view --json zvlyxwvk
jj-stack submit zvlyxwvk
```

## Handle commands that make changes

`submit`, `merge`, `sync`, `unstack`, and `cleanup` can finish some work before encountering a
problem. A nonzero exit therefore does not mean that nothing happened. Preserve the command's
output and inspect the repo again before retrying.

Two cases deserve particular care:

- `merge` may merge pull requests on GitHub and then fail while performing a `sync` (e.g. due to
  a network failure). In this case, rerunning `merge` will not restart a `sync`; it will notice
  that the merge has already completed and stop. Instead, run `sync` to complete the remaining
  cleanup work.
- `sync` rebases the changes with remaining pull requests onto trunk before updating those PRs.
  A `jj` rebase can complete with conflicted changes; if that happens, `sync` keeps the local
  rebase but cannot update the PRs from those changes. It therefore stops before updating the
  remaining pull requests or cleaning up the merged PRs. Resolve the conflicts with `jj`, then
  run the `submit` command printed by `sync`. A later `cleanup` can remove any leftover PR
  branches and tracking data.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | The command completed successfully. |
| 1 | A general failure, or `in-use` found that jj-stack is not used here. |
| 2 | The selection is not a supported stack. |
| 3 | Unresolved conflicts block the requested operation. |
| 4 | GitHub authentication, network, or API failure. |
| 5 | Invalid command-line arguments. |
| 6 | A selector matched more than one target. |
| 10 | `view` or `list` printed an incomplete report. |
| 11 | `in-use` could not determine the answer. |
| 130 | The command was interrupted. |

Exit codes classify the outcome; command output explains the particular problem and what to do
next.
