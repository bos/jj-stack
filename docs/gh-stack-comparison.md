# `jj-stack` and `gh stack`

Both [`jj-stack`](../README.md) and [`gh stack`](https://github.com/github/gh-stack) create
native GitHub stacks: one pull request per reviewable step, with each PR based on the one below
it. They can both submit, inspect, and merge those stacks.

They solve the same GitHub problem for different local version-control models. They are not
sensible alternatives inside the same working copy:

- In a `jj` repository, use `jj-stack` and author the stack as mutable `jj` changes.
- In a Git repository, `gh stack` can manage the stack as a chain of Git branches.

Running `gh stack` directly in a `jj` repository is technically possible, but wildly cumbersome.
You would have to create one local Git branch per PR, keep those branches matched to rewritten
`jj` changes, and maintain the PR base chain yourself. That is all bookkeeping that `jj-stack`
automates.

## How the models differ

| Topic | `jj-stack` | `gh stack` |
|---|---|---|
| Local source of truth | The `jj` DAG | An ordered list of Git branches |
| Review unit | One mutable `jj` change | One branch with one or more commits |
| Saved local state | PR, branch, and commit per change | Order, trunk, PRs, and rebase state |
| Review branches | Remote-only and normally hidden locally | Local and remote working branches |
| Restructure | `jj new`, `split`, `squash`, and `rebase` | `gh stack init`, `add`, and `modify` |
| Navigate | `jj` commands and workspaces | `gh stack` navigation commands |
| Refresh PRs | Submit after a `jj` rewrite | Rebase higher branches; push or submit |
| Submission UI | Flags, an editor, or helper programs | A full-screen editor, or `--auto` |
| Remote checkout | Adopt by PR and edit its change | Discover, create branches, and switch |
| Multiple stacks | From the DAG; shown by `list` | Explicit lists in `.git/gh-stack` |

In `jj-stack`, Git branches are machinery for GitHub, and are not how you organize local work.
In `gh stack`, the branches and their saved order are the local stack.

## Equivalent everyday tasks

| Task | `jj-stack` workflow | `gh stack` workflow |
|---|---|---|
| Start a stack | Create a series of `jj` changes | `gh stack init`, then `gh stack add` |
| Inspect it | `jj-stack view` | `gh stack view` |
| Publish or refresh it | `jj-stack submit` | `gh stack submit` |
| Edit a lower step | Edit with `jj`; submit | Switch, edit, then rebase higher branches |
| Merge the bottom part | `jj-stack merge` | `gh stack merge` |
| Apply an external merge locally | `jj-stack sync <head-change-id>` | `gh stack sync` |
| Remove the GitHub grouping | `jj-stack unstack` | `gh stack unstack` |
| Remove old artifacts | `jj-stack cleanup` | `gh stack sync --prune` for merged branches |

The cleanup row is not an exact equivalence. `jj-stack cleanup` removes managed review branches,
overview comments, and saved PR links after it can prove they are no longer needed. `gh stack`
works with your ordinary branches, so it treats branch pruning and removal of stack tracking as
separate operations.

### The two `sync` commands do different jobs

While many `jj-stack` commands are very similar to their `gh stack` equivalents, the shared
`sync` command name is worth attention.

`jj-stack sync <head-change-id>` applies completed GitHub merges to one selected local stack and
refreshes the PRs that remain. It:

- fetches current trunk and review state
- verifies which reviewed changes reached `trunk()`
- removes those merged changes from the local stack
- rebases the remaining selected changes when necessary
- updates their existing PRs

It is most useful after GitHub merges part of a stack. It never creates a PR, and it does not
rebase merely because trunk advanced. Use `jj rebase` for ordinary local history maintenance.

`gh stack sync` is a general whole-stack kitchen sink synchronization command. It fetches from
the remote, reconciles local and GitHub stack membership, updates trunk, rebases the branch
chain, pushes the branches, refreshes PR state, and can prune merged local branches.

## Using `gh stack link` with another local tool

`gh stack link` can create or extend a GitHub stack from existing branch names or PR numbers
without saving a local `gh stack`. It is useful when another tool already owns a stable branch per
PR and you only need GitHub grouping.

It is not a substitute for `jj-stack` in a `jj` workflow. You would still have to expose one Git
branch per change, keep those branches mapped to the right changes after rewrites, push them, and
keep the PR bases correct. `jj-stack` owns that bookkeeping. A stable `jj` change ID remains
connected to its PR even when the change is rewritten or renamed.

## Can the tools manage the same stack?

I do not recommend trying to mix `jj-stack` and `gh stack` in a single jj repo. Commands such as
`gh stack rebase`, `push`, `sync`, and `submit` expect to manage the review branches directly.
As an example, using `gh stack` to move a branch that `jj-stack` manages would cause `jj-stack`
to stop rather than overwrite the unexpected remote state.

That said, the GitHub review and merge experience *is* the same. The whole point is that you can
review a `jj-stack` stack in GitHub, and a merge performed in the GitHub UI or by another client
can be reconciled afterward with:

```bash
jj-stack sync <head-change-id>
```
