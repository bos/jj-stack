# JSON Output

`jj-stack view --json` and `jj-stack list --json` print structured versions of the
normal command output. The JSON schema uses the same user-facing concepts as the text
output: stacks, rows, changes, review bookmarks, pull requests, and status.

Command failures and incomplete GitHub inspection still use the normal CLI contract:
stderr explains the problem and the process exits non-zero. The JSON payload is not an
error-reporting format.

## Change Objects

Stack changes use this shape:

```json
{
  "change_id": "zvlyxwvksmry...",
  "bookmark": "review/add-json-output-zvlyxwvk",
  "subject": "add json output",
  "status": "open",
  "pull_request": {
    "number": 12,
    "url": "https://github.com/octo-org/example/pull/12"
  }
}
```

`current: true` is present when that change is the current working-copy change. It is
omitted otherwise.

`pull_request` is present when `jj-stack` knows the matching PR identity. It contains PR
identity, not a duplicate status summary; use the change's `status` field for review
state.

Known change statuses are:

- `unsubmitted`: no PR has been submitted for this change
- `submitted`: submitted before, but live GitHub status is unavailable
- `open`: open, non-draft PR with no review decision to report
- `draft`: open draft PR
- `approved`: open PR whose latest review state is approved
- `changes_requested`: open PR with requested changes
- `commented`: open PR with review comments but no approval or requested changes
- `merged`: PR is merged and local cleanup may be needed
- `closed`: PR is closed without being merged
- `missing`: saved PR identity exists, but GitHub did not report that PR for the branch
- `ambiguous`: more than one matching PR was found
- `unlinked`: the change was explicitly detached from review tracking
- `divergent`: multiple visible revisions exist for the same change
- `unknown`: GitHub lookup failed for this change

## `view --json`

`view --json` returns the selected stack or stacks:

```json
{
  "stacks": [
    {
      "selector": "PR 12",
      "changes": [
        {
          "change_id": "zvlyxwvksmry...",
          "bookmark": "review/add-json-output-zvlyxwvk",
          "subject": "add json output",
          "status": "open",
          "pull_request": {
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

`list --json` returns the same row model as the text table. Stack rows contain their
changes, so clients can derive the head change, change count, and PR list directly from
the `changes` array.

```json
{
  "rows": [
    {
      "type": "stack",
      "current": true,
      "subject": "add json output",
      "status": "open",
      "changes": [
        {
          "change_id": "zvlyxwvksmry...",
          "bookmark": "review/add-json-output-zvlyxwvk",
          "subject": "add json output",
          "status": "open",
          "pull_request": {
            "number": 12,
            "url": "https://github.com/octo-org/example/pull/12"
          }
        }
      ]
    },
    {
      "type": "orphan",
      "change_id": "kkkkkkkkkkkk...",
      "bookmark": "review/old-change-kkkkkkkk",
      "subject": "local change missing",
      "status": "orphan",
      "pull_request": {
        "number": 7,
        "url": "https://github.com/octo-org/example/pull/7"
      }
    }
  ]
}
```

`current: true` on a stack row means that the current working-copy change is part of
that stack. It is omitted for other stack rows.
