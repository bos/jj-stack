# Internal notes

This directory contains design documents and implementation notes for the
`jj-stack` codebase. It is written primarily by and for agents working on
the project. Most users of the tool will never need to read any of this.

If you are looking for how to use `jj-stack`, see the [user guide](../README.md).

## Contents

- **[design.md](design.md)** — the single canonical product specification. Read it before
  changing user-visible behavior.
- **[implementation-strategy.md](implementation-strategy.md)** — how the tool
  is built: component structure, tooling, test strategy.
- **[code-reviews.md](code-reviews.md)** — how to approach reviews for code
  and docs in this repo, including keeping review comments focused on real
  regressions, user surprise, and missing test evidence.
- **[testing-philosophy.md](testing-philosophy.md)** — what tests to write
  and how to evaluate them.
- **[distributed-state.md](distributed-state.md)** — the independently moving local `jj`, remote
  ref, and GitHub systems, their legal transitions, and required behavior per drift class.
- **[property-testing.md](property-testing.md)** — fixed and expanded property scenarios,
  invariants, and the runner.
- **[backlog.md](backlog.md)** — deferred features, open design questions,
  and non-blocking follow-up items. It is not a product specification.

Only [design.md](design.md) defines product behavior. The other documents explain implementation,
testing, review practice, or deferred work. Accepted plans guide their scoped work but do not
change behavior until the design is updated.
