# jj-stack

`jj-stack` sends a linear stack of local `jj` changes to GitHub as a stack of dependent pull
requests.

It is built for a rewrite-heavy review workflow made up of many small changes. Split a feature
into a few local parts, keep editing your changes in `jj`, and let `jj-stack` keep the matching
GitHub PR stack up to date.

## Quick start

### Requirements

- Python 3.14 or newer
- `uv`
- `jj` 0.39.0 or newer
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

If you are unsure what `jj-stack` will do, inspect first:

```bash
jj-stack
```

This is a synonym for `jj-stack view`.

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

On first submit, `jj-stack` creates one review bookmark per change. By default these bookmarks
look like `review/...`. They are normal `jj` bookmarks, and they are also the GitHub PR
branches. `jj-stack` manages them for you, so most of the time you do not need to move or
rename them yourself.

Inspect your stack again:

```bash
jj-stack
```

At this point you should have one GitHub PR per local change, with each PR based on the
review branch below it. Edit your changes locally with `jj`, run `jj-stack submit`
again, and the PR stack will be refreshed.

If you are juggling more than one local review stack in the same repo:

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
5. Once the bottom changes are approved, run `jj-stack land`.
6. If lower changes were merged on GitHub instead of with `jj-stack land`, run
   `jj-stack cleanup --rebase` when status says cleanup is needed.

`land` pushes the ready changes at the bottom of your stack to GitHub trunk and forgets
the local review bookmarks for the landed changes. It stops before the first change that
is not ready to land.

`cleanup --rebase` is helpful when some lower changes were merged on GitHub, for example with a
squash merge, and your local stack still contains those old merged ancestors. It removes those
merged ancestors from the local stack and rebases the remaining changes onto `trunk()`.

When `list` or `view` says a tracked stack changed since the last submit, inspect that
stack directly:

```bash
jj-stack view <head-change-id>
```

The status output will show whether the next step is a plain submit or cleanup first.

If `list` shows an `orphan` row, a PR is still open but the local change it reviewed is
no longer part of any current stack. When you are ready to retire that PR:

```bash
jj-stack unstack --cleanup --pull-request <pr>
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

A few repair and housekeeping commands are hidden by default:

```bash
jj-stack help --all
```

Like `jj`, `jj-stack` accepts `--color=always|never|debug|auto`. Without that flag, it
follows your `jj` `ui.color` setting.

## Configuration

For most use, `jj-stack` needs no configuration. It derives `git`, `jj`, and GitHub
information directly from `git`, `jj`, and `gh` whenever possible.

Repo-level config can be helpful for defaults such as reviewers and labels:

```toml
[jj-stack]
bookmark_prefix = "bos"
reviewers = ["octocat"]
labels = ["needs-review"]
use_bookmarks = ["potato/*", "spam/eggs"]
```

If you leave `bookmark_prefix` unset, `jj-stack` keeps the default `review/...` prefix.

`jj-stack submit` can override those defaults with `--reviewers`, `--team-reviewers`,
`--label`, and `--use-bookmarks`.

`cleanup_user_bookmarks` defaults to `false`. Leave it unset if bookmarks selected
through `use_bookmarks` should be preserved during later cleanup. Set it to `true` only
if you want `cleanup`, `unstack --cleanup`, and `land` to delete those reused bookmarks too
when that cleanup is otherwise safe.

For authentication, `jj-stack` checks `GH_TOKEN`, then `GITHUB_TOKEN`, then falls back
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
manages the GitHub projection and the local review bookmarks, and that's it.

## Why use it with coding agents?

Like people, coding agents produce better, more easily reviewed work when a task is split
into smaller, self-contained steps.

Any reviewer, human or not, will have an easier time with a series of incremental changes. This
matters even more when review feedback needs to be applied to one part of a stack without
obscuring the rest of the work.

- Agents work best when tasks are decomposed. A stacked review lets an agent revise only
  the commits that are wrong, and their descendants as needed, then resubmit.

- Smaller PRs are far easier for both humans and agents to re-read after feedback.
  Context windows are bigger in 2026, but agent attention is still limited, and human
  attention feels under ever more strain.

- Validation is more easily staged. It's easier to approve and land good changes while others
  are still in flux.

- Mutable local history is more valuable with agents. Agent-produced first drafts often need
  reshaping, and `jj` is the best tool to rework changes and history before refreshing GitHub.

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
- the test suite covers most workflows, with around 520 tests and greater than 80% coverage as
  of June 2026
- performance has been a major focus, with close attention to concurrent and batched
  operations to hide costs such as roundtrips to the GitHub API

## Focus and future

`jj-stack` is intentionally focused:

- `jj` has best-in-class mutable history
- `jj-stack` is GitHub only, at least for now
- linear stacks only
- one PR per change ID

GitHub is developing its own stacked review support, currently in limited preview. That model
appears compatible with `jj-stack`'s current model. Once stacked review support launches more
widely, I'll be able to test the API and server-side merge/rebase behaviour, and quickly support
it in `jj-stack` with minimal change to the CLI UX.
