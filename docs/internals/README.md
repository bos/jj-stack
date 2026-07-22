# Internal notes

This directory contains design documents and implementation notes for the
`jj-stack` codebase. It is written primarily by and for agents working on
the project. Most users of the tool will never need to read any of this.

If you are looking for how to use `jj-stack`, see the [user guide](../README.md).

## Authority and contents

- **[design.md](design.md)** — the single canonical product specification. Read it before
  changing user-visible behavior.
- **[implementation-strategy.md](implementation-strategy.md)** — how the tool
  is built: component structure, tooling, test strategy.
- **[code-reviews.md](code-reviews.md)** — how to approach reviews for code
  and docs in this repo, including keeping review comments focused on real
  regressions, user surprise, and missing test evidence.
- **[testing-philosophy.md](testing-philosophy.md)** — what tests to write
  and how to evaluate them.
- **[distributed-state.md](distributed-state.md)** — the four independently
  moving sources of state (local `jj`, remote refs, GitHub PRs, tracking store),
  their legal transitions, and required behavior per drift class.
- **[property-testing.md](property-testing.md)** — fixed and expanded property scenarios,
  invariants, and the runner.
- **[merger-complexity-audit.md](merger-complexity-audit.md)** — why the first merger
  implementation was stopped and the hard budgets governing its replacement.
- **[backlog.md](backlog.md)** — deferred features, open design questions,
  and non-blocking follow-up items. It is not a product specification.
- **[help-and-docs-plan.md](help-and-docs-plan.md)** — plan for making built-in help and `docs/`
  match the user's mental model. Shrinks as items ship.

Only [design.md](design.md) defines product behavior. The other documents explain implementation,
testing, review practice, or deferred work. Accepted plans guide their scoped work but do not
change behavior until the design is updated. Completed reports and superseded plans are historical
evidence only.
