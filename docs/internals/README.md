# Internal notes

This directory contains design documents and implementation notes for the
`jj-review` codebase. It is written primarily by and for agents working on
the project. Most users of the tool will never need to read any of this.

If you are looking for how to use `jj-review`, see the [user guide](../README.md).

## Contents

- **[design.md](design.md)** — canonical product spec: what the tool is
  supposed to do and why. Read this before changing any user-visible behavior.
- **[implementation-strategy.md](implementation-strategy.md)** — how the tool
  is built: component structure, tooling, test strategy.
- **[code-reviews.md](code-reviews.md)** — how to approach reviews for code
  and docs in this repo, including keeping review comments focused on real
  regressions, user surprise, and missing test evidence.
- **[testing-philosophy.md](testing-philosophy.md)** — what tests to write
  and how to evaluate them.
- **[backlog.md](backlog.md)** — deferred features, open design questions,
  and non-blocking follow-up items.
- **[help-and-docs-plan.md](help-and-docs-plan.md)** — plan for bringing
  built-in help and `docs/` to parity with `gt` and `gh stack`. Shrinks as
  items ship.
