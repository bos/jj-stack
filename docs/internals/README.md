# Internal notes

This directory contains design and implementation notes for contributors to `jj-stack`. Most
users of the tool will not need them.

If you are looking for how to use `jj-stack`, see the [user guide](../README.md).

## Suggested reading order

- Start with the summary and core concepts in **[design.md](design.md)**. It is the single
  product specification and must be read before changing user-visible behavior.
- Read **[implementation-strategy.md](implementation-strategy.md)** for state authority,
  execution boundaries, mutation ordering, and complexity policy.
- Read **[testing-philosophy.md](testing-philosophy.md)** before changing tests.

## Reference documents

- **[code-reviews.md](code-reviews.md)** — how to approach reviews for code
  and docs, with emphasis on real regressions and unnecessary complexity.
- **[property-testing.md](property-testing.md)** — the generated integration harness and how to
  run it.
Only [design.md](design.md) defines product behavior. The other documents explain implementation,
testing, or review practice. Accepted plans guide their scoped work but do not change behavior
until the design is updated.
