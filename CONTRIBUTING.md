# Contributing

Thanks for your interest in `jj-stack`.

## Before you start

Open an issue before writing a substantial change. `jj-stack` is deliberately opinionated about
what it manages and what it leaves to `jj`, so a feature that fits your workflow may still be out
of scope. [Compare tools](docs/tool-comparison.md) explains the stance and its trade-offs.

Bug reports, documentation corrections, and small focused fixes are welcome without prior
discussion.

## Development setup

You need `uv`, Git, `jj` 0.45.1 or newer, and `just`. The project requires Python 3.14 or newer;
`uv` can provide the interpreter when it creates the locked development environment:

```console
just setup
```

Run `just` on its own to list every recipe.

## Working on a change

Run the CLI from your checkout with `just run ...` rather than invoking the module or the
virtualenv path directly:

```console
just run view
```

Before finishing a code change, run the standard Ruff, type-check, and test pass. Docs-only edits
under `docs/` do not require it:

```console
just check
```

Pyrefly is the default type checker, including a second pass targeting Windows. To select ty
for the current platform instead, use `--type-checker` or its short form, `-t`:

```console
just check -t ty
```

Repeat the option to run several in the given order, for example `just check -t pyrefly -t ty`.
The runner installs the selected checkers from the lockfile, checks `src`, `tests`, `tools`, and
`check.py`, and runs the usual Ruff and pytest checks. It stops at the first failed check. ty is
optional during development and can report issues that Pyrefly does not; its failures are
reported normally. Plain `just check` and CI continue to use Pyrefly. Before a release, follow the
[requirement for both type checkers to pass](docs/internals/releasing.md#qualify-the-candidate).

mypy is deliberately not part of the toolchain. It fixes a variable's type at its first
assignment and rejects a later assignment of another type, which Pyrefly and ty accept, so
keeping it green required annotations whose only purpose was to satisfy mypy.

Run focused tests while you iterate:

```console
just test tests/unit/test_jj_client.py
```

CI also enforces the cumulative complexity budgets. Check them locally with `just complexity`
when a supported `tokei` is installed. Raising a budget is a design decision, not routine
maintenance — say why in the pull request.

If your change touches user-facing guides or references, refresh the website snapshot with
`just website` and include the resulting change in the sibling `website` repository.

## Conventions

[`AGENTS.md`](AGENTS.md) is the working agreement for this repo, and it applies to human
contributors too. It covers commit message format, the 98-column wrap, when documentation
changes are required, and where each kind of documentation belongs. The internal notes under
[`docs/internals/`](docs/internals/) cover the design, testing philosophy, and review standard.

The short version:

- Write commit subjects as `scope: summary`, lowercase, no trailing period.
- Explain *why* the change exists in the body, wrapped at 72 columns.
- Add tests at the narrowest layer that covers a distinct risk; consolidate overlapping coverage
  rather than adding parallel cases.
- For code changes, update docs when supported rules, workflows, or existing statements change.
  Documentation corrections and editorial improvements are welcome independently.

## Submitting

Pull requests go to `main`. `jj-stack` is developed with `jj-stack`, so a stacked series of small
changes is easy to review and very welcome.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
