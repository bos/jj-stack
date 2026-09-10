# jj-stack: manage stacked GitHub PRs with jj

`jj-stack` turns a linear series of local `jj` changes into a stack of GitHub pull requests.
Rewrite, split, squash, or reorder the changes with `jj`, then run `jj-stack submit` to update
GitHub. Existing PRs follow their change IDs, keeping comments and review history together.

## Quick start

You need Python 3.14 or newer, `jj` 0.45.1 or newer, and a repo on github.com where you can push
branches and open pull requests. `jj-stack` uses `GITHUB_TOKEN`, then `GH_TOKEN`, then your GitHub
CLI login for authentication.

Install with `uv`:

```bash
uv tool install jj-stack
```

### Submit your first stack

Start with a linear series of local `jj` changes on top of `trunk()`. Authenticate with
`gh auth login`, or supply a token in `GITHUB_TOKEN` or `GH_TOKEN`. Check the repo setup and
apply the safe local fixes:

```bash
jj-stack doctor --fix
```

Inspect the stack that ends at your working copy:

```bash
jj-stack
```

Create one GitHub PR per local change:

```bash
jj-stack submit
```

Revise the changes locally with `jj` and rerun `jj-stack submit` whenever the stack is ready to
refresh. Use `jj-stack list` to see every tracked stack in the repo.

## Mental model

Your local `jj` history determines which changes form a stack and their order. On GitHub, each
change gets a stable PR branch and a PR. The bottom PR targets trunk by default, and each PR
above it targets the PR branch below:

```text
Local changes:  trunk() <- A     <- B     <- C
GitHub PRs:     main    <- PR #1 <- PR #2 <- PR #3
```

Each PR's diff shows only the changes it adds on top of its base, so reviewers can consider
one change at a time. `jj-stack` manages the PR branches for you, and they normally stay out
of your local bookmark view.

When you rewrite a change, its change ID still connects it to the same PR. Submitting again
updates that PR and the PRs for any dependent changes, preserving their discussions.

To select a stack, pass the change ID of its head (the top change). `jj-stack` follows the
head's parents back to trunk to find the rest. If you don't name a head, it uses your working
copy when it has both a description and changes, or its parent otherwise. After editing a
lower change, pass the head's ID to `jj-stack submit` so the update includes the changes above
your edit.

## Everyday workflow

1. Write code as a series of local `jj` changes.
2. Run `jj-stack submit`.
3. Revise, add, remove, or reorder the changes locally as reviews come in.
4. Run `jj-stack submit` again to refresh GitHub.
5. Run `jj-stack merge --pull-request <last-pr-to-merge> --method squash` when the bottom
   portion is ready. Choose a merge method your repo allows. Queues choose their own method.
6. After a queued merge or a merge made through GitHub finishes, run
   `jj-stack sync <head-change-id>`.

`view`, `submit`, `merge`, and `sync` accept a change ID when you need to select a stack other
than the one ending at the working copy.

See the [user guide](https://www.serpentine.com/software/jj-stack/) for drafts, descriptions,
merge queues, cleanup, and working with multiple stacks.

## Optional setup

### Invoke it as `jj stack`

Add a command alias to your user configuration with `jj config edit --user`:

```toml
[aliases]
stack = ["util", "exec", "--", "jj-stack"]
```

For tab completion of both `jj-stack` and `jj stack`, add the output of `jj-stack completion` to
your shell startup file:

```bash
eval "$(jj-stack completion zsh --jj-alias stack)"
```

`bash` and `fish` work the same way. See
[Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/) for more
setup options.

### Other installation options

`pipx` provides another isolated installation:

```bash
pipx install jj-stack
```

You can also use `pip` inside an activated virtual environment:

```bash
python -m pip install jj-stack
```

To upgrade an installation made with `uv`, run `uv tool upgrade jj-stack`. If the command is
not on your shell `PATH`, run `uv tool update-shell`.

## Learn more

- [How jj-stack works](https://www.serpentine.com/software/jj-stack/mental-model/)
- [Submit and update](https://www.serpentine.com/software/jj-stack/guides/submit-and-update/)
- [Merge and sync](https://www.serpentine.com/software/jj-stack/guides/merge-and-sync/)
- [Multiple stacks](https://www.serpentine.com/software/jj-stack/guides/multiple-stacks/)
- [Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/)
- [Writing PR descriptions](https://www.serpentine.com/software/jj-stack/reference/descriptions/)
- [Troubleshooting](https://www.serpentine.com/software/jj-stack/troubleshooting/)
- [Compare tools](https://www.serpentine.com/software/jj-stack/tool-comparison/)
- [Automation](https://www.serpentine.com/software/jj-stack/reference/automation/)

For all flags and aliases, use the built-in help:

```bash
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

## Coding agent integration

Install the bundled skill to give coding agents instructions for working with `jj-stack`:

```bash
gh skill install bos/jj-stack jj-stack
```

The [skill source](skills/jj-stack/SKILL.md) and [evaluation notes](evals/jj-stack-skill.md) are
included in this repo.

## Development

With `uv`, `jj`, and `just` installed, run `just` to list the development workflows. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup and validation instructions.
