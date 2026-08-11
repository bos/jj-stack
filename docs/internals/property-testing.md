# Generated integration testing

The generated harness targets failures that emerge only when the local `jj` DAG, remote review
branches, GitHub, and local tracking disagree. It supplements focused tests; it is not a second
product specification.

## Required test boundary

Generated scenarios use real `jj` commands, a real Git repository, the CLI entry point, and the
fake GitHub server. A pure model may predict the result, but it cannot replace replay through
those boundaries. The expensive bugs are incorrect PR identity, branch targets, bases, or
recovery—not presentation differences or private request ordering.

The harness must check these properties where applicable:

- A surviving change keeps its PR and approvals across rewrites.
- A new change receives a new PR. An abandoned review keeps its PR, branch, and tracking until the
  user closes it and runs cleanup.
- Review branches and PR bases match the current `jj` DAG after a successful submit.
- A successful update never transiently closes, merges, reopens, or replaces an existing review.
- Unsafe external drift stops before local rewrites, pushes, GitHub mutations, or tracking writes
  and reports the expected error category.
- `view` reports a reachable drifted state or a targeted selection error instead of crashing.
- Retrying an interrupted submit reaches the intended state without duplicate PRs or lost review
  identity.

Merge and sync use a bounded lifecycle family rather than the general stack-edit generator. The
space is finite and policy-heavy, so focused cases are clearer than a large generated state
machine.

## Reproducibility and size

Each generated case has a stable ID, a compact operation trace, expected abstract state, and a
risk category. De-duplication may discard an equivalent final state only when the orphaned
reviews, rewritten changes, and risk category also match.

Generation uses explicit seeds, stable sorting, bounded stack sizes and trace lengths, and no
hash-order dependence. Every pytest worker must collect the same cases. If generation cannot find
the requested number of unique cases within its attempt limit, it returns the cases found instead
of looping indefinitely.

`./check.py` runs a small fixed set. Larger deterministic pools are opt-in:

```console
$ tests/run_submit_property_scenarios.py 500
```

The runner prints a complete reproduction command with the resolved seed, family counts, worker
count, and extra pytest arguments. Its `--help` is the reference for runner syntax. Pytest
arguments must follow a literal `--`.

When a generated scenario catches a real bug, keep a reduced representative in the fixed set, or
add a focused test if the failure belongs at a narrower boundary. Consolidate overlapping cases
and stay within the checked-in test and code-size limits.
