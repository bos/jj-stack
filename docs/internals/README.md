# Internal notes

This directory contains design and implementation notes for contributors to `jj-stack`. Most
users of the tool will not need them.

If you are looking for how to use `jj-stack`, see the [user guide](../README.md).

## Suggested reading order

- Start with the summary and core concepts in **[design.md](design.md)** for enduring product
  rules.
- Read **[implementation-strategy.md](implementation-strategy.md)** for state authority,
  execution boundaries, mutation ordering, and complexity policy.
- Read **[testing-philosophy.md](testing-philosophy.md)** before changing tests.

## Reference documents

- **[code-reviews.md](code-reviews.md)** — how to approach reviews of code
  and docs, with emphasis on real regressions and unnecessary complexity.
- **[property-testing.md](property-testing.md)** — the generated integration harness and how to
  run it.
- **[releasing.md](releasing.md)** — release qualification and publication.
