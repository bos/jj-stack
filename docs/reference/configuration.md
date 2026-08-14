---
title: Configuration
description: Set repository defaults, authentication, review branch names, and shell completion.
navGroup: Look things up
weight: 90
---

For most repositories, `jj-stack` needs no configuration. It reads local changes through `jj` and
pull request status from GitHub.

## Repository defaults

Edit repository configuration with `jj config edit --repo`:

```toml
[jj-stack]
reviewers = ["octocat"]
team_reviewers = ["reviewers"]
labels = ["needs-review"]
merge_method = "squash"
```

- `reviewers` contains GitHub usernames.
- `team_reviewers` contains team slugs without the organization prefix.
- `labels` contains labels added on submit.
- `merge_method` is `merge`, `rebase`, or `squash`.

Command-line options override these defaults without removing existing reviewers or labels that
you omit.

## Review branch names

By default, the Git branches managed by jj-stack start with `jj-stack/`. Do not create your own
branches with that prefix. `jj-stack doctor --fix` normally keeps those branches out of local
bookmark output.

Set a different prefix before the first submit:

```console
jj config set --repo jj-stack.branch_prefix my-reviews
```

## Authentication

Authentication is checked in this order:

1. `GITHUB_TOKEN`
2. `GH_TOKEN`
3. `gh auth token`, when the GitHub CLI is installed and authenticated

## Invoke it as `jj stack`

Add a command alias to your `jj` configuration:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

Then install matching shell completion:

```console
eval "$(jj-stack completion zsh --jj-alias stack)"
```

`bash` and `fish` work the same way.
