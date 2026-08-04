# jj-stack: manage stacked GitHub PRs with jj

`jj-stack` manages a linear series of `jj` changes on GitHub as a stack of dependent pull
requests.

It is built for a rewrite-heavy review workflow made up of many small changes. Split a feature
into a series of nicely contained parts, keep editing your changes in `jj`, and let `jj-stack`
keep the matching GitHub PR stack up to date.

## Quick start

### Requirements

- Python 3.14 or newer
- `uv`
- `jj` 0.43.0 or newer
- GitHub authentication

### Install

`jj-stack` has not been released to PyPI yet, so install it from the repository:

```bash
uv tool install git+https://github.com/bos/jj-stack
```

To upgrade later, rerun that command with `--force`. After the first release,
`uv tool install jj-stack` and `uv tool upgrade jj-stack` will work instead.

If `jj-stack` is not on your shell `PATH`, run:

```bash
uv tool update-shell
```

For tab completion, add the output of `jj-stack completion` to your shell startup file, for
example:

```bash
eval "$(jj-stack completion zsh)"
```

`bash` and `fish` work the same way.

To invoke it as `jj stack ...` (mirroring GitHub's `gh stack ...`) add a jj alias:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

### Before your first submit

The happy path is a local `jj` stack of changes that is ready to become a set of GitHub PRs:

- you are in a `jj` repo with a GitHub remote
- `trunk()` resolves to the branch you want the bottom PR to target, usually `main`
- your stack is linear
- the changes you want to submit are visible and mutable in `jj`
- GitHub authentication works from this shell

`jj-stack doctor` checks these to ensure you're good to go (and the branch-namespace setup
below), and will identify a fix for anything it sees as missing. Run it once in a new repo:

```bash
jj-stack doctor --fix
```

(Without `--fix`, it will only report what it sees.)

It's easy to learn what `jj-stack` will do. Inspect first:

```bash
jj-stack
```

(This is a synonym for `jj-stack view`.)

`jj-stack` reserves the `jj-stack/` branch namespace for the git branches it manages remotely on
GitHub. `doctor --fix` adds a fetch exclusion that normally keeps those branches out of your local
bookmark view. A missing exclusion is a warning, not a reason for other commands to stop.

After that, ordinary `jj git fetch` and `git fetch` exclude `jj-stack/*` branches. Do not keep
your own branches under `jj-stack/`. If the remote had no fetch configuration at all,
`doctor --fix` also writes the default `+refs/heads/*` refspec so the exclusion has something to
exclude from.

If another tool or a custom fetch setting makes a review bookmark visible, `jj-stack` checks it
only when relevant. A bookmark exposing the exact saved review commit is accepted. After a local
rewrite, that commit is treated as the published version rather than a competing local change.
Unknown bookmarks are left alone, and `jj-stack` will not claim a colliding name for a new review.
Trunk, tags, and other untracked bookmarks still apply normally.

To undo the reservation, remove the exclusion from the Git repository backing `jj`, naming your
own remote and prefix if they are not the defaults:

```bash
git --git-dir "$(jj git root)" config --unset --fixed-value \
  remote.origin.fetch '^refs/heads/jj-stack/*'
```

To reserve a different namespace, set `branch_prefix` before your first submit. Changing it later
leaves branches already pushed under the old prefix outside the reserved namespace, where
`jj-stack` will neither update nor delete them:

```bash
jj config set --repo jj-stack.branch_prefix my-reviews
```

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

For a multi-change stack, you can use `--describe stack=stack-overview.md` to post an overview of
the whole stack as a comment on the head PR. This is very helpful to orient a reviewer. See
[Writing PR descriptions](docs/description-helpers.md) for the other ways to set titles and
bodies and choose which PRs are drafts.

To review every title and body and choose the draft state of each PR in one editor document:

```bash
jj-stack submit --edit
```

Each change section has a `JJ: Draft: yes` or `JJ: Draft: no` field. The short forms `y` and `n`
work too. An invalid value aborts the submit before anything is pushed or changed on GitHub.

On first submit, `jj-stack` creates one stable, readable GitHub review branch per change, such as
`jj-stack/add-the-api-qpvuntsm`. The branches normally stay on the Git remote rather than
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

The unit of review is one local `jj` change. The local `jj` DAG determines which changes are in
the stack and their order.

On GitHub:

- each `jj` change gets one review branch
- each review branch gets one PR
- each PR targets the review branch below it, except the bottom PR, which targets trunk

For example:

```text
jj-stack/add-ui-...        -> PR #3 (base: jj-stack/add-api-...)
jj-stack/add-api-...       -> PR #2 (base: jj-stack/refactor-model-...)
jj-stack/refactor-model... -> PR #1 (base: main)
main                       -> trunk
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
6. After GitHub reports them merged, run `jj-stack sync <head-change-id>` to reconcile local
   history. A direct merge prints this command; after queueing, wait for GitHub first.

`merge` asks GitHub to merge the consecutive open, non-draft PRs at the bottom of the stack. It
requires every candidate to remain at the exact commit last submitted, but GitHub decides
approvals, checks, conflicts, and repository policy. GitHub merges a selected multi-PR bottom
portion as one operation. The same asynchronous API handles a one-PR review. If the trunk branch
requires a merge queue, GitHub accepts the selected PRs into the queue instead of merging them
immediately; `view` and `list` show them as queued.

`merge` never pushes trunk, rewrites local history, or removes review tracking. A queued result is
successful but does not mean trunk changed, so wait for GitHub to merge it. Then run
`jj-stack sync <head-change-id>`. It rebases the remaining
selected changes onto `trunk()`, updates only PRs that already exist for them, and cleans up a
merged PR when no local path still needs it. Conflicts remain local for you to resolve before
their PRs are updated. Ordinary `jj` rewrite propagation may also rebase local descendants, but
`sync` updates reviews only for the selected stack. Preview it with
`jj-stack sync --dry-run <head-change-id>`; if a rebase is needed, the later PR-update plan is
available only after you run `sync`.

When `list` or `view` says a tracked stack changed since the last submit, inspect that
stack directly:

```bash
jj-stack view <head-change-id>
```

The status output will show whether the next step is `jj-stack submit` or
`jj-stack sync <head-change-id>`.

If `list` shows an `orphan` row, tracking remains for a PR whose local change is no longer part
of any current stack. Close it on GitHub or with `gh`, then remove the review branch, stack
overview comment, and saved PR link it left behind:

```bash
gh pr close <pr>
jj-stack cleanup --pull-request <pr> --dry-run
jj-stack cleanup --pull-request <pr>
```

Use `--pull-request orphans` to preview or clean up every orphan in one operation:

```bash
jj-stack cleanup --pull-request orphans --dry-run
jj-stack cleanup --pull-request orphans
```

If GitHub still groups PRs that your local history now puts on different paths, remove that
grouping without closing the PRs:

```bash
jj-stack unstack --stack <number>
```

To sweep review branches, stack overview comments, and tracking that no closed or merged review
still needs, across the whole repository:

```bash
jj-stack cleanup --dry-run
jj-stack cleanup
```

`cleanup` leaves open reviews alone. If a repository ever looks wrong, `jj-stack doctor` checks
its setup and GitHub access and names a fix for what it finds.

## Learn more

User guides live under [docs](docs/README.md):

- [Mental model](docs/mental-model.md)
- [Daily workflow](docs/daily-workflow.md)
- [Writing PR descriptions](docs/description-helpers.md)
- [Troubleshooting](docs/troubleshooting.md)
- [JSON output](docs/json-output.md)
- [Exit codes](docs/exit-codes.md)

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

Repo-level defaults save repeating the same flags. Set them with `jj config edit --repo`:

```toml
[jj-stack]
reviewers = ["octocat"]
team_reviewers = ["reviewers"]
labels = ["needs-review"]
merge_method = "squash"
```

- `reviewers` are GitHub usernames, and `team_reviewers` are team slugs as GitHub spells them,
  without the organization prefix.
- `merge_method` is `merge`, `rebase`, or `squash`. Set it when the repository allows more than
  one: GitHub reports which methods it allows but not which one you want, so without it
  `jj-stack merge` asks you to pass `--method` every time. A method the repository does not
  allow is refused before anything is sent to GitHub.
- `branch_prefix` names the reserved branch namespace, described under
  [Before your first submit](#before-your-first-submit).

`jj-stack submit` can override the reviewer and label defaults with `--reviewers`,
`--team-reviewers`, and `--label`, and `jj-stack merge` overrides `merge_method` with
`--method`.

Passing `--reviewers` or `--team-reviewers` also applies those review requests when the pull
requests are otherwise unchanged. Existing reviewers that are omitted are left in place.

For authentication, `jj-stack` checks `GITHUB_TOKEN`, then `GH_TOKEN`, then falls back
to `gh auth token` if `gh`, the GitHub CLI, is installed and authenticated.

## Why use it

The standard GitHub code review model gets awkward once a feature wants to be reviewed as a
series of dependent steps, especially when intermediate steps need revision.

While you could model that with plain Git branches, the bookkeeping quickly becomes unwieldy.
`jj-stack` takes a different approach:

- your local `jj` DAG determines the stack
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
`jj` stack:

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

`jj-stack` registers every multi-PR review as a GitHub stack and asks GitHub to merge
the selected prefix together. A review with one PR remains an ordinary pull request.
