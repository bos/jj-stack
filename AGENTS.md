# Complexity control

- A replacement is incomplete until it deletes the mechanism it supersedes in the same change.
  Do not keep a parallel model for a later cleanup.
- Store each persistent fact in one form, with one owner. Shared observation and storage code
  must not introduce competing policy decisions.
- Batch independent read-only facts, but keep dependent mutations in order. Bind an irreversible
  external mutation to the identity and version observed while planning when the platform
  supports a conditional write or lease. Re-observe only when an earlier mutation invalidates a
  precondition or when an observed trigger or platform contract requires it.
- CI enforces the limits in `complexity-budget.toml`. Run `just complexity` before finishing a
  code change when a supported `tokei` is installed. Remaining headroom is not worth reporting.
- When a change written the natural way exceeds a limit, ask for an increase: name the limit, the
  overage, and why the growth is needed. Do not reshape code, merge tests, or delete coverage to
  fit, and do not edit the budget file until the increase is approved. Never lower a limit to
  match current usage.
- If the same subsystem needs a third consecutive hardening change, stop patching it and
  re-derive the design from the core invariants.

# Workflow

- This is a `jj` repo. Do not use `git` to work on the repo itself.
- Do not use git worktree-based agent isolation in this repo. For isolated parallel work, use
  `jj workspace` instead.
- Run the CLI locally with `just run ...` instead of invoking the module or virtualenv path
  directly.
- Hard-wrap code and markdown files at 98 columns unless a file uses a different convention.
  Release-note prose follows `docs/internals/releasing.md` and is intentionally unwrapped.

# Commit messages

- Format the first line as a concise scoped subject, usually `scope: summary`.
- Match the repo's existing subject style: use a lowercase scope such as `status`, `docs`, or
  `cli`, followed by a short lowercase phrase, with no trailing period.
- Use a body for any change whose purpose is not obvious from the subject and diff.
- Hard-wrap commit message bodies at 72 columns.
- The body should explain the motivation for the change, the intended behavior or design outcome,
  and any important scope or design constraints.
- Do not use the body to narrate the code or to record routine validation such as `just check`.
- Prefer explaining why the commit exists and what rule or user-visible behavior it is enforcing.

# Documentation

- User-facing docs live in `docs/`, except for contributor notes in `docs/internals/`. See
  [docs/AGENTS.md](docs/AGENTS.md) for the vocabulary rules and the public/internal split.
  Built-in `--help` text is held to the same standard as the user docs: assume jj/git familiarity,
  avoid `jj-stack` internal design jargon.
- The web version of the user docs normally lives in the sibling jj repository at
  `$(jj root)/../website`. When user-facing docs change here, run `just website`, inspect the
  corresponding website changes, and update them as needed. If the change here is committed,
  commit the corresponding website update in that repository as well; preserve unrelated work in
  either working copy.
- Active internal docs use ordinary technical language too. Introduce a project-specific term
  only when it names an existing type, field, or enduring rule, define it at first use, and prefer
  describing concrete inputs and effects.
- Internal design and strategy documents describe the current product and architecture. Keep
  implementation history in `jj` commits.
- For implementation work, update documentation when a change adds or alters a supported rule or
  workflow, or makes an existing statement inaccurate. A bug fix alone does not require a docs
  change. Documentation reviews and corrections can also address clarity, duplication, and gaps.
- When documentation is required, update only the affected source: `design.md` for an enduring
  product rule, user docs or `--help` for user guidance, and the relevant testing document for
  test strategy.

# Behaviour changes

- In user-facing output, identify revisions by `change_id` by default. If a concrete immutable
  snapshot matters, include the `commit_id` second and label it explicitly.
- Read [docs/internals/design.md](docs/internals/design.md) before intentionally changing product
  semantics or adding tests for product behavior. Design prose is derived from principles, not
  the other way around: evaluate a documented behavior on its merits before extending it, and
  prefer deleting case-specific rules that follow from the principles over adding new ones. Never
  add durable transaction or replay state; recovery is observational (see design.md).
- Preserve the core invariants: the `jj` DAG determines stack topology, tracking stores only PR
  links and submitted commits, GitHub PRs follow the local stack, and ambiguous linkage stops
  mutation.

# Testing

- Run `just check` for the default local Ruff, type-check, and test pass before finishing a code
  change. Docs-only edits under `docs/` do not require a test run.
- Run focused tests with `just test`, for example `just test tests/unit/test_jj_client.py`.
- Before adding, modifying, removing, or reviewing tests, fixtures, helpers, or property
  scenarios, read and follow
  [docs/internals/testing-philosophy.md](docs/internals/testing-philosophy.md). Add or retain
  coverage only for a distinct worthwhile risk at the narrowest meaningful layer; search for and
  consolidate overlapping coverage first. For property-harness changes, also follow
  [docs/internals/property-testing.md](docs/internals/property-testing.md).

# Code reviews

- When reviewing changes or existing code, read and follow
  [docs/internals/code-reviews.md](docs/internals/code-reviews.md).
