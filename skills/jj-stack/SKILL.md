---
name: jj-stack
license: Apache-2.0
description: >
  Manage jj-native stacked GitHub review with jj-stack. Use when an agent needs
  to inspect, create, submit, refresh, revise, land, clean up, or recover stacked
  pull requests for local jj changes, especially in repos that use jj change IDs,
  bookmarks, trunk(), and mutable history.
---

# jj-stack

`jj-stack` sends a linear chain of local `jj` changes to GitHub as dependent
pull requests. Use `jj` to edit the local stack. Use `jj-stack` to inspect,
submit, refresh, land, and clean up the matching GitHub PRs.

## Agent Rules

1. **Edit stacks with `jj`, not Git branches.** Use `jj` to create, split,
   squash, reorder, rebase, and rewrite local changes.
2. **Use `jj-stack` for GitHub review state.** Use it to inspect PR status,
   create or refresh PRs, land ready bottom changes, and clean up review
   branches.
3. **Select stacks with change IDs or explicit revsets.** Prefer stable
   `change_id` values in user-facing summaries. Use commit IDs only when a
   concrete immutable snapshot matters.
4. **Inspect before mutating.** Run `jj-stack view` or `jj-stack list` before
   `submit`, `land`, `cleanup`, or `unstack` unless the user gave a precise
   command.
5. **Preview risky operations.** Use `--dry-run` before `submit`, `land`,
   `cleanup --rebase`, and `unstack --cleanup` when the next step is uncertain.
6. **Do not manage review branches by hand.** `jj-stack` creates and updates the
   review bookmarks it uses as GitHub PR branches.
7. **Do not use `gh stack` for a `jj-stack` stack.** `gh stack` is branch-based
   and keeps different local state.
8. **Avoid interactive helpers.** Do not use an interactive `--describe-with`
   helper unless the user asks for it.
9. **Honor the repo's local invocation.** In the `jj-stack` repo itself, run
   `uv run jj-stack ...`; in normal installed use, run `jj-stack ...` or the
   configured `jj stack ...` alias.

Never run these for a `jj-stack` stack unless the user explicitly asks:

- `git branch`, `git checkout`, `git rebase`, or manual Git branch pushes
- `gh stack ...`
- `jj-stack unstack --cleanup` without first confirming that the PRs should be
  closed and the review branches removed

## Inspect First

Use `view` before mutating a stack:

```bash
jj-stack view
jj-stack view --json
jj-stack view --fetch --json
jj-stack view <head-change-id>
jj-stack view --pull-request <pr>
```

Use `list` for a repo-wide inventory:

```bash
jj-stack list
jj-stack list --json
jj-stack list --fetch --json
```

Use JSON output when another tool or script will consume status. Use `--fetch`
when current remote branch positions matter.

## Quick Reference

- Inspect the current stack: `jj-stack view`
- Inspect with machine-readable output: `jj-stack view --json`
- List known stacks: `jj-stack list`
- Preview submit: `jj-stack submit --dry-run`
- Create or refresh PRs: `jj-stack submit`
- Ask previous reviewers to look again: `jj-stack submit --re-request`
- Preview landing ready bottom changes: `jj-stack land --dry-run`
- Land ready bottom changes: `jj-stack land`
- Connect existing PRs to local changes: `jj-stack checkout --pull-request <pr> --fetch`
- Clean up after squash/rebase merges on GitHub: `jj-stack cleanup --rebase`
- Close PRs for an abandoned stack: `jj-stack unstack`
- Close an orphaned PR and remove review state:
  `jj-stack unstack --cleanup --pull-request <pr>`

## Stack Structure

Each local `jj` change should be a small reviewable unit. Put foundational
changes lower in the stack and dependent changes higher in the stack:

```text
trunk()
  refactor shared model      -> PR #1
    add API endpoint         -> PR #2
      add UI                 -> PR #3
        add integration test -> PR #4
```

Plan the stack before writing a large change. If one change depends on another,
the dependency belongs in the same change or in a lower change. If the work is
unrelated, use a separate `jj` stack.

## Submit Workflow

1. Build or revise the local stack with `jj`.
2. Check the selected stack with `jj-stack view`.
3. Preview GitHub changes with `jj-stack submit --dry-run`.
4. Submit or refresh PRs with `jj-stack submit`.
5. Use `jj-stack submit --re-request` only when the user wants prior reviewers
   asked to look again.

The default submit target is the current completed stack head, usually `@-`.
After an interrupted command, ambiguous status, or multi-stack work, pass an
explicit revset, change ID, or `--pull-request` selector instead of relying on
the default.

## Revising a Stack

When review feedback requires a lower change:

1. Move to or edit the appropriate `jj` change.
2. Make the fix in that change, then rebase or let descendants follow according
   to normal `jj` workflow.
3. Run `jj-stack view` to confirm the stack shape.
4. Run `jj-stack submit --dry-run`, then `jj-stack submit`.

Do not patch a higher change just to avoid changing a lower dependency. Review
works best when each local `jj` change remains the unit that reviewers should
read.

## Landing and Cleanup

Preview landing first unless the user has already asked to land:

```bash
jj-stack land --dry-run
jj-stack land
jj-stack land --pull-request <pr>
```

`land` lands the consecutive ready changes at the bottom of the stack. It stops
before the first unready change.

Use `cleanup --rebase` only when lower changes were merged on GitHub through
different commit IDs, such as a squash merge, and the local stack still contains
those old merged ancestors:

```bash
jj-stack cleanup --rebase --dry-run
jj-stack cleanup --rebase
```

If `trunk()` merely advanced, use plain `jj rebase` instead of
`cleanup --rebase`.

Use `unstack` only when the user wants to stop reviewing a stack:

```bash
jj-stack unstack --dry-run
jj-stack unstack
jj-stack unstack --cleanup --pull-request <pr>
```

The `--cleanup` form also removes managed review branches, local bookmarks, and
tracking data, so use it only when the stack should be retired.

## Existing PR Stacks

When the PRs already exist and local `jj-stack` tracking is missing, connect to
them before submitting:

```bash
jj-stack checkout --pull-request <pr> --fetch
jj-stack checkout --revset <revset> --fetch
```

`checkout` sets up local tracking. It does not rewrite commits, rebase changes,
or modify GitHub.

## Failure Handling

- If a command is interrupted, inspect with `jj-stack view` and rerun the
  intended command with an explicit change ID, revset, or PR selector.
- If `jj-stack` reports ambiguity, stop and ask for a concrete selector rather
  than guessing which PR or branch to update.
- If `jj` reports a stale workspace, run `jj workspace update-stale`.
- Use `jj op log` and `jj undo` for local jj recovery. Do not use destructive
  Git commands in a jj repo.
- If authentication or remote resolution is unclear, run `jj-stack doctor`.
