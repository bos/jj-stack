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
  moving state-holders (local `jj`, remote refs, GitHub PRs, tracking store),
  their legal transitions, and required behavior per drift class.
- **[property-testing.md](property-testing.md)** — fixed and expanded property scenarios,
  invariants, and the runner.
- **[merger-complexity-audit.md](merger-complexity-audit.md)** — why the first merger
  implementation was stopped and the hard budgets governing its replacement.
- **[backlog.md](backlog.md)** — deferred features, open design questions,
  and non-blocking follow-up items. It is not a product specification.
- **[help-and-docs-plan.md](help-and-docs-plan.md)** — plan for bringing
  built-in help and `docs/` to parity with `gt` and `gh stack`. Shrinks as
  items ship.

Implementation strategy and evidence-policy documents are subordinate to the product
specifications. An explicitly accepted implementation plan governs its scoped work until its
decisions are folded into the product specification. Critiques, superseded plans, and completed
merger reports remain historical evidence rather than behavioral authority.
