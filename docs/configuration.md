# Configuration

For most use, `jj-stack` needs no configuration. It reads repository and change information
through `jj`, and reads review state from GitHub.

## Repository defaults

Set repository-level defaults with `jj config edit --repo`:

```toml
[jj-stack]
reviewers = ["octocat"]
team_reviewers = ["reviewers"]
labels = ["needs-review"]
merge_method = "squash"
```

- `reviewers` contains GitHub usernames.
- `team_reviewers` contains team slugs without the organization prefix.
- `labels` contains labels to add when submitting PRs.
- `merge_method` is `merge`, `rebase`, or `squash`. Set it when the repository allows more than
  one method; GitHub reports which methods it allows but not which one you prefer.

`jj-stack submit` can override these defaults with `--reviewers`, `--team-reviewers`, and
`--label`. Passing reviewer options also applies those review requests when the PRs are otherwise
unchanged; existing reviewers that are omitted remain in place.

`jj-stack merge --method` overrides `merge_method`. Without either setting, `merge` asks you to
choose when the repository allows multiple methods. It refuses a method the repository does not
allow before sending anything to GitHub.

## Review branch namespace

`jj-stack` reserves the `jj-stack/` branch namespace for the Git branches it manages on GitHub.
Do not keep your own branches there. `jj-stack doctor --fix` sets up a `git fetch` exclusion in
your repo's config that normally keeps these branches out of your local bookmark view.

If another tool exposes a review bookmark, `jj-stack` handles the common case. After you rewrite
a submitted change locally, the bookmark still points to its published commit. `jj-stack`
recognizes that commit instead of treating it as a competing copy of the change. Other bookmarks
still follow normal `jj` immutability rules.

To use a different namespace than `jj-stack/`, set `branch_prefix` before the first submit:

```bash
jj config set --repo jj-stack.branch_prefix my-reviews
```

`jj-stack` uses the configured value as-is before the slash in each generated review branch.

## Authentication

`jj-stack` checks `GITHUB_TOKEN`, then `GH_TOKEN`, then falls back to `gh auth token` when the
GitHub CLI is installed and authenticated.

## `jj stack` command alias

To invoke `jj-stack` as `jj stack`, add a `jj` command alias:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

If you use an alias, replace the ordinary `jj-stack completion` line in your shell setup as
follows, which will allow `jj stack` to complete properly:

```bash
eval "$(jj-stack completion zsh --jj-alias stack)"
```

If you want to name your alias something else, e.g. `stk`, simply use that name in both the
`jj` config and your shell init script.

`bash` and `fish` work the same way. The generated script completes both `jj-stack` and
`jj stack`, while leaving other `jj` commands with their existing completion. If no `jj`
completion is installed, it uses `jj`'s dynamic completion as the fallback.
