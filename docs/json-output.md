# JSON Output

`jj-stack view --json` and `jj-stack list --json` print structured versions of the
normal command output. The JSON schema uses the same user-facing concepts as the text
output: stacks, rows, changes, review branches, pull requests, and status.

The checked-in schema is [json-output.schema.json](json-output.schema.json).
Integration tests validate real command output against that file.

Command failures and incomplete GitHub inspection still use the normal CLI contract:
stderr explains the problem and the process exit code says what kind of problem it was
(see [exit-codes.md](exit-codes.md)). In particular, `view --json` and `list --json`
print a valid payload and exit 10 when the report is incomplete. The JSON payload is not
an error-reporting format.

## Change Objects

Stack changes use this shape:

```json
{
  "change_id": "zvlyxwvksmry...",
  "branch": "review/add-json-output-zvlyxwvk",
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

`branch` is present only when tracking data attaches the change to an exact review branch.
An unsubmitted change has no `branch` field; `jj-stack` does not generate a speculative name
for status output. An orphan row always has one, because saved tracking is the only thing that
identifies it.

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
          "branch": "review/add-json-output-zvlyxwvk",
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
          "branch": "review/add-json-output-zvlyxwvk",
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
      "branch": "review/old-change-kkkkkkkk",
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

A stack row's `status` is a human-readable summary such as the counts of open, approved, or
unsubmitted changes. Its wording is not a stable machine-readable vocabulary. Scripts should
inspect the `changes` array and use each change's documented `status` value instead. An orphan
row always uses `"status": "orphan"`.
