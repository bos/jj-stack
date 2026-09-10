---
title: Configuration
description: Set repo defaults, Git remote selection, authentication, and shell completion.
navGroup: Look things up
weight: 90
---

Most repos need no jj-stack-specific configuration. The settings below let you choose defaults
for submitting and merging, or use jj-stack through a `jj stack` alias.

## Repo defaults

Edit repo configuration with `jj config edit --repo`:

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
- `merge_method` is `merge`, `rebase`, or `squash`. Without it or `--method`, immediate merges
  use the only allowed method or prefer rebase, then squash, then merge. When several methods
  are allowed, signed commits require an explicit choice because merging can discard their
  signatures. Merge queues choose their own method. See
  [merge methods](../guides/merge-and-sync.md#choose-a-merge-method) for signature considerations.

Command-line options override the corresponding defaults for that invocation. Omitting an
existing reviewer or label does not remove it from a PR.

## PR branch names

By default, PR branches start with `jj-stack/`. Reserve this prefix for jj-stack. Run
`jj-stack doctor --fix` if those branches appear in local bookmark output.

Set a different prefix before the first submit:

```console
jj config set --repo jj-stack.branch_prefix my-prs
```

This gives PR branches names beginning with `my-prs/`.

## Git remote

jj-stack uses the `origin` remote when it exists, or the repo's only Git remote otherwise. The
remote's fetch and push URLs must contain the same `owner/repo` path.

You need push access to that repo. jj-stack pushes PR branches to the same repo that receives
the pull requests, and GitHub stacks cannot include branches from a fork. Working from a fork
against a repo you cannot push to is not supported.

SSH hostname aliases such as `git@github-work:owner/repo.git` are supported. The alias must
connect to `github.com`: jj-stack takes the `owner/repo` path from the remote URL and always
uses GitHub's public API. GitHub Enterprise Server is not supported.

## Authentication

For GitHub API requests, jj-stack uses the first available token from:

1. `GITHUB_TOKEN`
2. `GH_TOKEN`
3. `gh auth token`, when the GitHub CLI is installed and authenticated

Pushing PR branches uses your Git remote's authentication: an SSH key for an SSH URL, or Git's
HTTPS credentials for an HTTPS URL. Setting `GITHUB_TOKEN` or `GH_TOKEN` supplies the API token;
jj-stack does not configure Git credentials from it.

`jj-stack doctor` checks API access and your permission to push to the repo without attempting
a push. If it succeeds but `submit` cannot push, check the credentials for your remote's push
URL.

## Logging

`jj-stack` logs warnings and errors to standard error. To see what a command is doing, pass
`--debug`, which enables debug logging for that run. To change the default level instead, set:

```toml
[jj-stack.logging]
level = "INFO"
```

`level` accepts a Python logging level name such as `DEBUG`, `INFO`, or `WARNING`.

## Invoke it as `jj stack`

Add a command alias to your `jj` configuration:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

## Shell completion

To complete commands such as `jj stack s<TAB>`, generate completion with `--jj-alias stack`.
This connects the alias to jj-stack's commands and options while preserving other `jj`
completions.

Add the appropriate line to your shell's startup file **after** any existing `jj` completion
setup. In Zsh, it must also follow completion initialization (`compinit`).

For Bash (`~/.bashrc`):

```bash
eval "$(jj-stack completion bash --jj-alias stack)"
```

For Zsh (`~/.zshrc`, after your completion initialization):

```zsh
eval "$(jj-stack completion zsh --jj-alias stack)"
```

For Fish (`~/.config/fish/config.fish`):

```fish
jj-stack completion fish --jj-alias stack | source
```

These lines enable completion for both `jj-stack` and `jj stack`. Omit `--jj-alias stack` if you
only use the standalone `jj-stack` command. Start a new shell after editing the startup file, or
run the line in your current shell to enable completion immediately.
