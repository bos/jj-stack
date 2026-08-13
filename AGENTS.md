# Work in progress

This project is under heavy development. Do not make any attempt to write backwards
compatibility code, migration code, or the like.

## Complexity control

- A replacement is incomplete until it deletes the mechanism it supersedes in the same change.
  Do not add a temporary parallel model with a promise to remove it in a later cleanup slice.
- Define each jj-stack-owned durable policy fact once and store it one way. Shared observation or
  storage code must not create a second path for deciding or changing it.
- Batch independent read-only facts, but keep dependent mutations in order. Bind an irreversible
  external mutation to the identity and version observed while planning when the platform
  supports a conditional write or lease. Re-observe only when an earlier mutation invalidates a
  precondition or when an observed trigger or platform contract requires it.
- Apply the cumulative complexity budgets after every code slice. CI runs
  `uv run tools/check_complexity.py`; run it locally when the pinned `tokei` is installed. A
  budget increase is a design stop that requires explicit review, not routine maintenance of the
  budget file.
- If the same subsystem needs a third consecutive hardening change, stop patching it and
  re-derive the design from the core invariants.

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
- Hard-wrap commit message bodies at 72 columns.
- The body should explain the motivation for the change, the intended behavior or design outcome,
  and any important scope or design constraints.
- Do not use the body to narrate the code or to record routine validation such as `./check.py`.
- Prefer explaining why the commit exists and what rule or user-visible behavior it is enforcing.

# Documentation

- User-facing docs live in `docs/`. See [docs/AGENTS.md](docs/AGENTS.md) for the vocabulary
  rules and the public/internal split. Built-in `--help` text is held to the same standard as
  the user docs: assume jj/git familiarity, avoid `jj-stack` internal design jargon.
- Active internal docs use ordinary technical language too. Introduce a project-specific term
  only when it names a real type, field, or enduring rule, define it at first use, and prefer
  describing concrete inputs and effects.
- `design.md` and `implementation-strategy.md` describe the current product and architecture, not
  completed slices or abandoned mechanisms. Keep implementation history in `jj` commits.

# Behaviour changes

- In user-facing output, identify revisions by `change_id` by default. If a concrete immutable
  snapshot matters, include the `commit_id` second and label it explicitly.
- Read [docs/internals/design.md](docs/internals/design.md), the single canonical product spec,
  before changing behavior or adding tests. Design prose is derived from principles, not
  the other way around: evaluate a documented behavior on its merits before extending it,
  and prefer deleting case-specific rules that follow from the principles over adding new
  ones. Never add durable transaction or replay state; recovery is observational
  (see design.md).
- Preserve the core invariants: the `jj` DAG determines stack topology, local cache is sparse,
  GitHub pull requests are derived from the local `jj` stack, and ambiguous linkage fails closed.
- If behavior changes, update `design.md` and the user docs in the same change and make sure tests
  pass. Update `implementation-strategy.md` only for an architecture, tooling, or test-layer
  change; use `jj` commits for slice history.

# Testing

- Run `./check.py` before finishing a code change. Docs-only edits under `docs/` do not require
  a test run.
- Run `./check.py` for the default local Ruff, type-check, and test pass before finishing a
  code change.
- For focused test runs, do not use plain `uv run pytest ...`; it can miss the repo's package
  path in this project layout. First run `uv sync --locked`, then invoke pytest through the repo
  virtualenv, for example `.venv/bin/python -m pytest tests/unit/test_jj_client.py`.
- Before adding, modifying, removing, or reviewing tests, fixtures, helpers, or property
  scenarios, read and follow
  [docs/internals/testing-philosophy.md](docs/internals/testing-philosophy.md). Add or retain
  coverage only for a distinct worthwhile risk at the narrowest meaningful layer; search for and
  consolidate overlapping coverage first. For property-harness changes, also follow
  [docs/internals/property-testing.md](docs/internals/property-testing.md).

# Code reviews

- When reviewing changes or existing code, read and follow
  [docs/internals/code-reviews.md](docs/internals/code-reviews.md).
