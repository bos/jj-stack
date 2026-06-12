# Work in progress

This project is under heavy development. Do not make any attempt to write backwards
compatibility code, migration code, or the like.

# Workflow

- This is a `jj` repo. Do not use `git` to work on the repo itself.
- Do not use git worktree-based agent isolation in this repo. For isolated parallel work, use
  `jj workspace` instead.
- Run the CLI locally with `uv run jj-stack ...` instead of invoking the module or virtualenv
  path directly.
- Hard-wrap code and markdown files at 98 columns unless a file uses a different convention.

# Commit messages

- Format the first line as a concise scoped subject, usually `scope: summary`.
- Match the repo's existing subject style: use a lowercase scope such as `status`, `docs`, or
  `cli`, followed by a short lowercase phrase, with no trailing period.
- Use a body for any change whose purpose is not obvious from the subject and diff.
- The body should explain the motivation for the change, the intended behavior or design outcome,
  and any important scope or design constraints.
- Do not use the body to narrate the code or to record routine validation such as `./check.py`.
- Prefer explaining why the commit exists and what rule or user-visible behavior it is enforcing.

# Documentation

- User-facing docs live in `docs/`. See [docs/AGENTS.md](docs/AGENTS.md) for the vocabulary
  rules and the public/internal split. Built-in `--help` text is held to the same standard as
  the user docs: assume jj/git familiarity, avoid `jj-stack` internal design jargon.

# Behaviour changes

- In user-facing output, identify revisions by `change_id` by default. If a concrete immutable
  snapshot matters, include the `commit_id` second and label it explicitly.
- Read [docs/internals/design.md](docs/internals/design.md) and
  [docs/internals/implementation-strategy.md](docs/internals/implementation-strategy.md)
  before changing behavior or adding tests. `design.md` is the canonical
  product spec.
- Preserve the core invariants: the `jj` DAG is the source of truth, local cache is sparse,
  GitHub pull requests are derived from the local `jj` stack, and ambiguous linkage fails
  closed.
- If behavior changes, update the docs in the same change and make sure tests pass.
- Once a slice is implemented, update the implementation doc to note this.
- Non-blocking design debt, architecture follow-ups, and deferred ideas belong in
  [docs/internals/backlog.md](docs/internals/backlog.md).

# Testing

- Run `./check.py` before finishing a code change. Docs-only edits under `docs/` do not require
  a test run.
- Run `./check.py` for the default local Ruff, type-check, and test pass before finishing a
  code change.
- For focused test runs, do not use plain `uv run pytest ...`; it can miss the repo's package
  path in this project layout. First run `uv sync --locked`, then invoke pytest through the repo
  virtualenv, for example `.venv/bin/python -m pytest tests/unit/test_jj_client.py`.
- When adding, removing, or evaluating tests, read
  [docs/internals/testing-philosophy.md](docs/internals/testing-philosophy.md) first and follow
  it.

# Code reviews

- When reviewing changes or existing code, read and follow
  [docs/internals/code-reviews.md](docs/internals/code-reviews.md).
