# jj-stack

`jj-stack` sends a linear stack of local `jj` changes to GitHub as a stack of dependent pull
requests.

It is built for a rewrite-heavy review workflow made up of many small changes. Split a feature
into a series of nicely contained parts, keep editing your changes in `jj`, and let `jj-stack`
keep the matching GitHub PR stack up to date.

## Quick start

### Requirements

- Python 3.14 or newer
- `uv`
- `jj` 0.43.0 or newer
- GitHub authentication via `gh auth login`, `GH_TOKEN`, or `GITHUB_TOKEN`

### Install

```bash
uv tool install jj-stack
```

To upgrade later:

```bash
uv tool upgrade jj-stack
```

If `jj-stack` is not on your shell `PATH`, run:

```bash
uv tool update-shell
```

To invoke it as `jj stack ...` — mirroring GitHub's `gh stack ...` — add a jj alias:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

### Before your first submit

The happy path is a local `jj` stack that is ready to become a set of GitHub PRs:

- you are in a `jj` repo with a GitHub remote
- `trunk()` resolves to the branch you want the bottom PR to target, usually `main`
- your stack is linear
- the changes you want to submit are visible and mutable in `jj`
- GitHub authentication works from this shell

It's easy to learn what `jj-stack` will do. Inspect first:

```bash
jj-stack
```

(This is a synonym for `jj-stack view`.)

### Two-minute first run

Suppose you have a few local changes stacked on top of `trunk()`:

- refactor the shared model
- add the API
- add the UI

Preview the submit plan without changing anything:

```bash
jj-stack submit --dry-run
```

Submit the stack to GitHub:

```bash
jj-stack submit
```

`submit` also accepts the short alias `sub`.

If you have already written a PR body in a Markdown file, pass it when submitting:

```bash
jj-stack submit --describe <change-id>=pr-body.md
```

For a multi-change stack, you can use `--describe stack=stack-overview.md` to add an overview
description of the entire stack to the head PR. This is very helpful to orient a reviewer.

On first submit, `jj-stack` creates one GitHub review branch per change. Its readable name has
the fixed form `review/<subject-slug>-<short-change-id>`, for example
`review/add-the-api-qpvuntsm`. `jj-stack` keeps that name stable for the life of the review, even
if you rewrite the change or edit its subject. The branches stay on the Git remote rather than
appearing as persistent bookmarks in your local `jj` view.

Inspect your stack again:

```bash
jj-stack
```

At this point you should have one GitHub PR per local change, with each PR based on the
review branch below it. Edit your changes locally with `jj`, run `jj-stack submit`
again, and the PR stack will be refreshed.

If you are juggling more than one local stack in the same repo:

```bash
jj-stack list
```

`list` also accepts the short alias `ls`.

## Mental model

The unit of review is one local `jj` change. The local `jj` DAG is the source of truth
for which changes are in the stack and what order they are in.

On GitHub:

- each `jj` change gets one review branch
- each review branch gets one PR
- each PR targets the review branch below it, except the bottom PR, which targets trunk

For example:

```text
review/add-ui-...        -> PR #3 (base: review/add-api-...)
review/add-api-...       -> PR #2 (base: review/refactor-model-...)
review/refactor-model... -> PR #1 (base: main)
main                     -> trunk
```

When you rewrite an intermediate change in `jj`, `jj-stack` updates the matching review branch
and PR, along with the changes that depend on it, instead of asking you to maintain a stack of
Git branches by hand.

## Core workflow

Your typical author loop is:

1. Write code as a series of local `jj` changes.
2. Run `jj-stack submit`.
3. Revise those changes locally as reviews come in.
4. Re-run `jj-stack submit`.
5. Once the bottom changes are ready, run `jj-stack merge`.
6. Run the printed `jj-stack sync <head-change-id>` to reconcile local history.

`merge` asks GitHub to merge the consecutive open, non-draft PRs at the bottom of the stack. It
requires every candidate to remain at the exact commit last submitted, but GitHub decides
approvals, checks, conflicts, and repository policy. Repositories with GitHub stack support merge
the selected bottom portion as one operation; other repositories merge PRs bottom-up and stop at
the first rejection.

`merge` never pushes trunk, rewrites local history, or removes review tracking. Run
`jj-stack sync <head-change-id>` after GitHub merges lower changes. It rebases the remaining
selected changes onto `trunk()`, updates only PRs that already exist for them, and cleans up a
merged PR when no PR above still needs its review branch. Unreviewed trailing work stays local,
and other local stacks are left alone. Preview it with
`jj-stack sync --dry-run <head-change-id>`; if a rebase is needed, the later PR-update plan is
available only after you run `sync`.

When `list` or `view` says a tracked stack changed since the last submit, inspect that
stack directly:

```bash
jj-stack view <head-change-id>
```

The status output will show whether the next step is `jj-stack submit` or
`jj-stack sync <head-change-id>`.

If `list` shows an `orphan` row, tracking remains for a PR whose local change is no longer part of
any current stack. When you are ready to close it if needed and clean up its verified artifacts:

```bash
jj-stack unstack --cleanup --pull-request <pr> --dry-run
jj-stack unstack --cleanup --pull-request <pr>
```

Use `--pull-request orphans` to preview or clean up every orphan in one operation:

```bash
jj-stack unstack --cleanup --pull-request orphans --dry-run
jj-stack unstack --cleanup --pull-request orphans
```

## Learn more

User guides live under [docs](docs/README.md):

- [Mental model](docs/mental-model.md)
- [Daily workflow](docs/daily-workflow.md)
- [Troubleshooting](docs/troubleshooting.md)

The built-in help is the flag reference:

```bash
jj-stack --help
jj-stack submit --help
```

To include advanced repair commands and hidden global options:

```bash
jj-stack help --all
```

Like `jj`, `jj-stack` accepts `--color=always|never|debug|auto`. `always` forces color
even if `NO_COLOR` is set. Without that flag, `jj-stack` follows your `jj` `ui.color`
setting.

## Configuration

For most use, `jj-stack` needs no configuration. It reads repository and change information
through `jj`, and reads review state from GitHub.

Repo-level config can be helpful for defaults such as reviewers and labels:

```toml
[jj-stack]
reviewers = ["octocat"]
labels = ["needs-review"]
```

`jj-stack submit` can override those defaults with `--reviewers`, `--team-reviewers`,
and `--label`.

Passing `--reviewers` or `--team-reviewers` also applies those review requests when the pull
requests are otherwise unchanged. Existing reviewers that are omitted are left in place.

For authentication, `jj-stack` checks `GITHUB_TOKEN`, then `GH_TOKEN`, then falls back
to `gh auth token` if `gh`, the GitHub CLI, is installed and authenticated.

## Why use it

The standard GitHub code review model gets awkward once a feature wants to be reviewed as a
series of dependent steps, especially when intermediate steps need revision.

While you could model that with plain Git branches, the bookkeeping quickly becomes unwieldy.
`jj-stack` takes a different approach:

- your local `jj` DAG is the source of truth for the stack
- history stays mutable in `jj`
- GitHub gets the review branches and PRs it needs
- when you modify an intermediate change, `jj-stack` does the PR and branch wrangling

The key point is that you get to keep thinking in terms of local logical changes. `jj-stack`
manages the GitHub branches, pull requests, and their small amount of local tracking, and that's
it.

## Why use it with coding agents?

Like people, coding agents produce better, more easily reviewed work when a task is split
into smaller, self-contained steps.

Any reviewer, human or not, will have an easier time with a series of incremental changes. This
matters even more when review feedback needs to be applied to one part of a stack without
obscuring the rest of the work.

- Agents work best when tasks are decomposed. A stacked review lets an agent revise only
  the changes that are wrong, and their descendants as needed, then resubmit.

- Smaller PRs are far easier for both humans and agents to re-read after feedback.
  Context windows are bigger in 2026, but agent attention is still limited, and human
  attention feels under ever more strain.

- Validation is more easily staged. It's easier to approve and merge good changes while others
  are still in flux.

- Mutable local history is more valuable with agents. Agent-produced first drafts often need
  reshaping, and `jj` is the best tool to rework changes and history before refreshing GitHub.

### AI agent integration

If you use coding agents, install the bundled `jj-stack` skill so they know how to work with a
`jj`-native stack:

```bash
gh skill install bos/jj-stack jj-stack
```

The skill is separate from the `uv` installation. It teaches agents to use `jj` for local stack
edits, read machine-readable status from `jj-stack`, and refresh GitHub through `submit`.

To install it for a specific agent or scope, pass the corresponding `gh skill install` flags.
For example, to install it for Codex at user scope:

```bash
gh skill install bos/jj-stack jj-stack --agent codex --scope user
```

When developing the skill locally, install from this checkout:

```bash
gh skill install . jj-stack --from-local --agent codex --scope user --force
```

The source skill lives at `skills/jj-stack/SKILL.md`.

## Performance

Although `jj-stack` is written in Python, this does not significantly affect its speed.
The real determinants of its performance are the GitHub API and the `jj` command.

The GitHub API is *slow*; a single roundtrip takes many hundreds of milliseconds. `jj-stack`
reduces its impact with:

- GraphQL batch requests where possible
- concurrent use of the GitHub REST API

`jj-stack` also batches calls to `jj` and minimizes the amount of work those calls must
do.

## Development note

This project has been developed with heavy coding agent assistance; almost all code is
agent-written. Nevertheless, I've provided heavy oversight.

- quality of the user experience is paramount
- user-facing docs are managed separately from generated implementation work
- the test suite covers the main workflows and failure modes
- performance has been a major focus, with close attention to concurrent and batched
  operations to hide costs such as roundtrips to the GitHub API

## Focus and future

`jj-stack` is intentionally focused:

- `jj` has best-in-class mutable history
- `jj-stack` is GitHub only, at least for now
- linear stacks only
- one PR per change ID

When GitHub exposes native stacked-review support for a repository, `jj-stack` registers submitted
PRs in GitHub's stack model and asks GitHub to merge them together. Otherwise, PR comments provide
stack navigation and `merge` submits eligible PRs bottom-up.
