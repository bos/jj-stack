# Daily workflow

This is the normal author loop `jj-review` is designed around.

## 1. Build your local stack with `jj`

Create some local changes that you want reviewed. For example:

- refactor the shared model
- add the API
- add the UI

Keep your stack linear (or rewrite it to be linear prior to review). `jj-review` is
intentionally focused on one linear stack at a time.

## 2. Inspect before submitting

`jj-review` will by default submit the current stack ending at `@-` (the most recent completed
change below your working directory). In the common case, this is the stack you just built on
top of `trunk()`. If `trunk()` has advanced since you last rebased, your stack instead starts
from an older ancestor of `trunk()`. `jj-review status` will show the ancestor in the footer
beneath your stack, so you can see exactly what the stack is based on.

You can easily check what the tool thinks that stack is:

```bash
jj-review
```

This is the same command as `jj-review status`. (`status` also accepts the short alias `st`.)

This is a good go-to command whenever you are unsure what your stack looks like or what you have
submitted for review.

In a large or busy project, you'll often be working on multiple stacks at a time. If you want a
repo-wide inventory of the stacks you have in flight, use the `list` command (or its short alias
`ls`):

```bash
jj-review list
```

When you run `jj log` directly, you may also notice review bookmarks. These bookmark names are
generated automatically. (By default they start with `review/...`, but you can configure a
different prefix for your repo.) These bookmarks get turned into git branches that `jj-review`
uses for GitHub PRs.

## 3. Submit the stack

Create or refresh the GitHub pull requests for the current stack:

```bash
jj-review submit
```

If you want to first inspect what `submit` *would* do, without making any changes:

```bash
jj-review submit --dry-run
```

If a change does not already have its review branch and PR set up, `jj-review submit` creates
the matching review bookmark for it. After that, it reuses that bookmark as the stable GitHub PR
head branch while you revise your local change.

## 4. Revise locally as reviews come in

During review, you can make any changes you want with `jj`. Split, squash, reorder, or rewrite
locally as needed.

Once the local stack looks right again, refresh GitHub:

```bash
jj-review submit
```

If you want to ask prior reviewers to take another look after you've addressed feedback, run:

```bash
jj-review submit --re-request
```

This will notify reviewers who approved or asked for changes to a PR.

## 5. Check readiness

Use `status` when you need to answer:

- which changes already have PRs
- which PRs are draft, approved, blocked, or need cleanup

If review state already exists on another machine or only on GitHub, run `jj-review import`
first to start working on that stack locally.

If you want to inspect the stack for one linked PR directly:

```bash
jj-review status --pull-request 7
```

(You can use `-p` as an alias for `--pull-request`.)

If you want to inspect several stacks in one run, pass several selectors in
the order you want them shown:

```bash
jj-review status foo --pull-request 7 bar
```

For more detail, pass `--verbose`:

```bash
jj-review status --verbose
```

## 6. Land the changes that are ready

When the bottom part of the stack is ready to land:

```bash
jj-review land
```

What does it mean for a change to be "ready"? Its state on GitHub must be:
- open
- not a draft
- approved by at least one reviewer
- no outstanding changes requested by any reviewer

And also, locally, we need the `jj` state to be clean:
- it has no unresolved conflicts
- it has not diverged

If you rebased a reviewed change without changing its diff, `land` refreshes the review branch
for you before it pushes `trunk()`. If you changed the diff since the last review, you'll need
to rerun `submit` first; this will update the PR to show your new content, so reviewers can take
another look.

If you want to preview the landing plan without actually landing your changes:

```bash
jj-review land --dry-run
```

If you want to land only up through one specific pull request:

```bash
jj-review land --pull-request 7
```

By default, a successful `land` forgets the local review bookmarks for the changes that actually
landed. Use `--skip-cleanup` if you want to keep those local review bookmarks.

`land` lands the consecutive run of ready PRs at the bottom of your stack. It stops as soon as
there's a change it cannot land, and will not land changes above a non-landable change. To land
mid-stack changes, use `jj arrange` or `jj rebase` to reorder your stack and move them to the
bottom first.

A successful `land` pushes your local git commit IDs directly to `trunk()`. If later local
changes remain above the landed changes, they will not need rebasing just because some changes
landed. If someone lands your changes through the GitHub UI, say using a squash merge, you might
need to rebase; read on.

## 7. Rebase remaining work

`jj-review cleanup --rebase` is specifically about removing merged ancestors from your local
stack and rebasing surviving descendants onto `trunk()`. Use it when some lower changes were
merged on GitHub through different commit IDs and your local stack still contains those
now-merged ancestors:

```bash
jj-review cleanup --rebase
```

`cleanup --rebase` does not otherwise rewrite history. If your stack simply drifted because
`trunk()` advanced without anything in your stack landing, rebase with plain `jj`:

```bash
jj rebase -s <bottom-of-stack> -d 'trunk()'
```

After `cleanup --rebase`, there might be open PRs for your remaining not-yet-landed changes on
GitHub that still point at old branch targets, old parent PRs, or old diffs. You can refresh
GitHub's view of your stack with:

```bash
jj-review submit
```

## 8. Close abandoned review stacks

If a stack should no longer be reviewed:

```bash
jj-review close
```

If it's handier to identify your stack by PR number, you can specify that instead:

```bash
jj-review close --pull-request 7
```

Use `--cleanup` when you also want to remove the stack's old review branches and `jj-review`'s
tracking data after the PRs are closed. If `jj-review` created local review bookmarks for those
branches, this will forget those too.

If `jj-review list` shows an `orphan` row, the PR is still open but its local change is no
longer part of any current stack. When you are ready to retire that PR, close it explicitly:

```bash
jj-review close --cleanup --pull-request 7
```

If `jj-review list` says another tracked stack changed since its last submit, either run
`jj-review submit <head-change-id>` to refresh the PR branches or run
`jj-review status <head-change-id>` to inspect first. `status` only emits this warning for another
stack when that stack is built on top of a change in the stack you are inspecting. Status calls
out whether local commits, review parents, or stack membership differ from the last successful
submit, and it will also show if cleanup is needed first.

## Short version

The steady-state loop is:

```bash
jj-review status
jj-review submit
# edit in jj
jj-review submit
jj-review land
jj-review cleanup --rebase
jj-review submit
```

## When something goes wrong

If a command is interrupted mid-way (crash, Ctrl-C, network failure), `status`
will report an interrupted operation, when it started, and whether it belongs
to the stack currently being shown. Use the command lines printed by `status`
to inspect or continue the recorded stack:

```bash
jj-review status              # see what was interrupted
jj-review status <change-id>  # inspect the interrupted stack
jj-review submit <change-id>  # finish an interrupted submit
jj-review abort --dry-run     # preview what abort would undo
jj-review abort               # undo and clean up, if the preview looks right
```

For interrupted `submit`, the recorded notice identifies the stack it started
from. Re-run `submit` or `close --cleanup` with an explicit revset for the
stack you want, not a naked command that falls back to the default selection.

If you rewrote that stack in the meantime, `abort` will not try to guess how to
undo the old partial submit.

See the [troubleshooting guide](troubleshooting.md) for more recovery scenarios.
