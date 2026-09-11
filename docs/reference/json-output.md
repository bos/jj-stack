---
title: JSON output
description: Read stable stack and pull request data from scripts and agents.
navGroup: Look things up
weight: 105
---

`jj-stack view --json` reports selected stacks. `jj-stack list --json` reports tracked stacks and
orphaned PRs in the repo. Both write JSON to standard output and diagnostics to standard error.

The published schema is
[json-output.schema.json](https://github.com/bos/jj-stack/blob/main/docs/json-output.schema.json).
Fields may be added; scripts should ignore fields they do not use. For schema validation, use
the schema from the same jj-stack release as the CLI.

An incomplete report is still valid JSON, but the command exits 10. Save both the output and exit
code so you can distinguish a complete report from a partial one. Other failures may produce no
JSON. See [Automation and agents](automation.md#keep-partial-reports) for an example and the full
exit-code reference.

## Change objects

Stack changes use this shape:

```json
{
  "change_id": "zvlyxwvksmry...",
  "branch": "jj-stack/add-json-output-zvlyxwvk",
  "subject": "add json output",
  "status": "open",
  "needs_submit": false,
  "needs_sync": false,
  "pr": {
    "checks": "passed",
    "number": 12,
    "url": "https://github.com/octo-org/example/pull/12"
  }
}
```

`change_id` is the full jj change ID. `subject` is the first line of its description.

`needs_submit` is true when local edits to a submitted change need publishing. It is false for
unsubmitted changes, queued PRs, divergence, and lookup or saved-link problems. It does not
establish that all requirements for submission are satisfied. `needs_sync` is true when the PR
has merged or jj-stack has confirmed that its submitted work reached trunk, and the change
remains in the reported local stack.

`current: true` is present when the change is the current working-copy change and omitted
otherwise.

When available, `reason` explains a problem with the change's PR and `repair` gives recovery
guidance. Both are plain text for display; their wording can change. Use `status` for decisions.

`branch` is present only when jj-stack has a saved pull request link for the change. Unsubmitted
changes omit it. Orphan rows always include it.

`pr` contains the pull request number, plus its URL and combined check result when available.
Use the change's `status` for the PR's state and review decision.

When jj-stack has only a saved PR number, `pr` contains `number` alone. This is the case for
`submitted` changes and orphan rows. `url` requires a live GitHub lookup; `checks` is included
only when GitHub reports a check result.

`checks` is `passed`, `failed`, or `pending`; `pending` includes checks that GitHub expects but
has not started.

Known change statuses are:

- `unsubmitted`: jj-stack has no saved pull request link for this change
- `submitted`: submitted before, but live GitHub status is unavailable
- `open`: open, non-draft PR with no review decision to report
- `queued`: open PR waiting in GitHub's merge queue
- `draft`: open draft PR
- `approved`: open PR whose latest review decision is approved
- `changes_requested`: open PR with requested changes
- `merged`: PR has merged or its submitted work has reached trunk; local cleanup may be needed
- `closed`: PR is closed without being merged
- `missing`: tracking data names a PR, but GitHub did not report that PR for the branch
- `ambiguous`: more than one matching PR was found
- `link_mismatch`: the saved PR has a different or missing head branch, or another PR uses it
- `branch_moved`: the PR head moved outside jj-stack, or GitHub and the remote branch disagree
- `divergent`: multiple visible commits exist for the same unmerged change
- `unknown`: GitHub lookup failed for this change

## `view --json`

`view --json` returns a `stacks` array. Within each stack, `changes` runs from the head down to
the bottom, matching the text display. `head_change_id` identifies the head, which is the
**first** entry. For an empty stack, it identifies the resolved selection.

```json
{
  "stacks": [
    {
      "selector": "PR 12",
      "head_change_id": "zvlyxwvksmry...",
      "changes": [
        {
          "change_id": "zvlyxwvksmry...",
          "branch": "jj-stack/add-json-output-zvlyxwvk",
          "subject": "add json output",
          "status": "open",
          "needs_submit": false,
          "needs_sync": false,
          "pr": {
            "checks": "passed",
            "number": 12,
            "url": "https://github.com/octo-org/example/pull/12"
          }
        }
      ]
    }
  ]
}
```

`selector` is present only when the stack came from an explicit selector such as a
revset argument or `--pull-request`.

## `list --json`

`list --json` returns a `rows` array. Each row has a `type` of `stack` or `orphan`.

In a stack row, `changes` runs from the bottom up to the head. `head_change_id` identifies the
head, which is the **last** entry, and the row's `subject` is that head's subject. This order is
the reverse of `view --json`.

An orphan row describes a saved pull request link whose local change is no longer part of a
current stack. It has its own `change_id`, `branch`, and optional `pr`, without a `changes` array.

```json
{
  "rows": [
    {
      "type": "stack",
      "head_change_id": "zvlyxwvksmry...",
      "current": true,
      "subject": "add json output",
      "status": "1 approved, open, checks pending",
      "changes": [
        {
          "change_id": "rlvmnowlqpsu...",
          "branch": "jj-stack/add-the-model-rlvmnowl",
          "subject": "add the model",
          "status": "approved",
          "needs_submit": false,
          "needs_sync": false,
          "pr": {
            "checks": "passed",
            "number": 11,
            "url": "https://github.com/octo-org/example/pull/11"
          }
        },
        {
          "change_id": "zvlyxwvksmry...",
          "branch": "jj-stack/add-json-output-zvlyxwvk",
          "subject": "add json output",
          "status": "open",
          "needs_submit": false,
          "needs_sync": false,
          "pr": {
            "checks": "pending",
            "number": 12,
            "url": "https://github.com/octo-org/example/pull/12"
          }
        }
      ]
    },
    {
      "type": "orphan",
      "change_id": "kkkkkkkkkkkk...",
      "branch": "jj-stack/old-change-kkkkkkkk",
      "subject": "local change missing",
      "status": "orphan",
      "pr": {
        "number": 7
      }
    }
  ]
}
```

`current: true` marks the stack associated with the working copy. It can mark the parent's stack
when `@` is an empty change above it. Other stack rows omit the field. To locate `@` itself, look
for `current: true` on an individual change.

A stack row's `status`, such as `1 approved, open, checks pending`, is a human-readable summary.
Its wording can change. Scripts should inspect the individual changes' documented `status`
values, even for a stack with only one change. An orphan row always uses `"status": "orphan"`.
