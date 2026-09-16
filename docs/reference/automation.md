---
title: Automation and agents
linkTitle: Automation and agents
description: >-
  Set up coding agents, inspect stacks from scripts, and handle command outcomes safely.
navGroup: Look things up
weight: 110
---

## Install the coding-agent skill

The bundled skill gives coding agents instructions for using jj-stack to manage pull requests
and PR branches. Install it with the
[GitHub CLI](https://cli.github.com/manual/gh_skill_install):

```bash
gh skill install bos/jj-stack jj-stack
```

To tell the agent when to load the skill, add this to your personal or repo agent instructions:

```markdown
## jj-stack

Before any GitHub pull request or branch task in a jj repo, run `jj-stack in-use`. If it
exits 0, load and follow the jj-stack skill. If it exits 1, continue without that skill. For any
other exit, stop and report the error. Cache the result for the repo during this session. Check
when the task arises, not at session startup.
```

`in-use` is a silent, read-only check for `jj-stack`'s local tracking data. Exit 0 means the
repo uses `jj-stack`, exit 1 means it does not, and exit 11 means the check itself failed.

## Inspect stacks from a script

Use `jj-stack view --json` to inspect a selected stack and `jj-stack list --json` to find all
tracked stacks:

```console
jj-stack view --json <head-change-id>
jj-stack list --json
```

Both commands write JSON to standard output and diagnostics to standard error. See
[JSON output](json-output.md) for the format and published schema.

`view` returns a `stacks` array. `list` returns a `rows` array containing both stacks and orphaned
PRs: saved pull request links whose local changes have left every current stack. Neither command
discovers stacks that exist only on GitHub.

For decisions in a script, inspect each change's documented `status` value, such as `open` or
`merged`. A stack row's `status` is a human-readable summary whose wording may change.

### Keep partial reports

`view` and `list` exit 10 when they can report some state but cannot produce a complete report.
With `--json`, standard output still contains valid JSON. Keep the report and record that it was
incomplete.

For example:

```sh
report=$(mktemp) || exit 1
trap 'rm -f "$report"' 0
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

# Parse "$report" here. "$result" is complete or incomplete.
```

Exit 0 means inspection completed, not that every stack is ready to merge. The JSON may still
contain closed pull requests, orphaned PRs, or other work that needs attention.

## Select the same stack reliably

Pass the head change ID explicitly so a command selects the intended stack even if the working
copy moves between commands.

The two JSON commands order their `changes` arrays differently:

- In `view`, the head is the **first** entry; changes run from head to bottom.
- In a `list` stack row, the head is the **last** entry; changes run from bottom to head.

Change IDs survive edits and rebases, so use them when passing a selection to a later command:

```console
jj-stack view --json zvlyxwvk
jj-stack submit zvlyxwvk
```

## Handle commands that make changes

Commands that make changes can complete some work before failing. Preserve their output and
inspect the repo again before retrying; a nonzero exit does not mean nothing happened.

Follow the recovery command printed by jj-stack. In particular:

- If `jj-stack sync` rebases changes into conflicts, follow
  [sync conflict recovery](../troubleshooting.md#sync-rebased-your-changes-into-conflicts) before
  publishing the remaining changes. This also applies to the automatic sync after a merge.
- `jj-stack merge` waits for GitHub to finish. In automation, pass `--no-wait` and run
  `jj-stack sync` in a later step.
- After GitHub finishes a merge, rerunning `jj-stack merge` does not resume the remaining work.

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
