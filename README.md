# jj-stack: manage stacked GitHub PRs with jj

`jj-stack` turns a linear series of local `jj` changes into a stack of GitHub pull requests.
Rewrite, split, squash, or reorder the changes with `jj`, then let `jj-stack` update the
matching PRs.

## Quick start

### Requirements

- Python 3.14 or newer
- `uv`
- `jj` 0.44.0 or newer
- GitHub authentication

### Install

Install `jj-stack` from PyPI:

```bash
uv tool install jj-stack
```

To upgrade, rerun that command with `--force`. If the command is not on your shell `PATH`, run
`uv tool update-shell`.

For tab completion, add the output of `jj-stack completion` to your shell startup file:

```bash
eval "$(jj-stack completion zsh)"
```

`bash` and `fish` work the same way.

To invoke it as `jj stack` and complete that alias too, see
[Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/).

### Submit your first stack

Start with a linear series of local `jj` changes on top of `trunk()`. In a new repository, check
the setup and apply the safe local fixes:

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
refresh. Use `jj-stack list` to see every tracked stack in the repository.

## Mental model

The local `jj` DAG determines which changes form a stack and their order. On GitHub, each change
gets a stable review branch and a PR; every PR targets the review branch below it, except the
bottom PR, which targets trunk by default:

```text
jj-stack/add-ui-...         -> PR #3 (base: jj-stack/add-api-...)
jj-stack/add-api-...        -> PR #2 (base: jj-stack/refactor-model-...)
jj-stack/refactor-model-... -> PR #1 (base: main)
main                        -> trunk
```

The review branches normally stay out of your local bookmark view. When you rewrite a change,
`jj-stack` updates its existing branch and PR, along with the changes that depend on it.

## Everyday workflow

1. Write code as a series of local `jj` changes.
2. Run `jj-stack submit`.
3. Revise, add, remove, or reorder the changes locally as reviews come in.
4. Run `jj-stack submit` again to refresh GitHub.
5. Run `jj-stack merge` when the changes at the bottom are ready.
6. After a queued or externally initiated merge finishes, run
   `jj-stack sync <head-change-id>`.

`view`, `submit`, `merge`, and `sync` accept a change ID when you need to select a stack other
than the one ending at the working copy.

See the [user guide](https://www.serpentine.com/software/jj-stack/) for drafts, descriptions,
merge queues, cleanup, and working with multiple stacks.

## Learn more

- [Mental model](https://www.serpentine.com/software/jj-stack/mental-model/)
- [Quick start](https://www.serpentine.com/software/jj-stack/quick-start/)
- [Everyday workflows](https://www.serpentine.com/software/jj-stack/guides/submit-and-update/)
- [Configuration](https://www.serpentine.com/software/jj-stack/reference/configuration/)
- [Writing PR descriptions](https://www.serpentine.com/software/jj-stack/reference/descriptions/)
- [Troubleshooting](https://www.serpentine.com/software/jj-stack/troubleshooting/)
- [`jj-stack` and `gh stack`](https://www.serpentine.com/software/jj-stack/gh-stack/)
- [JSON output](https://www.serpentine.com/software/jj-stack/reference/json-output/)
- [Automation and exit codes](https://www.serpentine.com/software/jj-stack/reference/automation/)

The built-in help is the canonical flag reference:

```bash
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

## Coding agent integration

Install the bundled skill to teach coding agents to work with local `jj` stacks and refresh their
GitHub PRs safely:

```bash
gh skill install bos/jj-stack jj-stack
```

See the [skill source](https://github.com/bos/jj-stack/blob/main/skills/jj-stack/SKILL.md).

## Performance

Although `jj-stack` is written in Python, this does not significantly affect its speed.
The real determinants of its performance are the GitHub API and the `jj` command.

The GitHub API is *slow*; a single roundtrip takes many hundreds of milliseconds. `jj-stack`
reduces its impact with:

- GraphQL batch requests where possible
- concurrent use of the GitHub REST API
- periodic audits that its queries are minimal in extent

In pursuit of good performance, `jj-stack` also batches calls to `jj` and minimizes the amount
of work those calls must do.
