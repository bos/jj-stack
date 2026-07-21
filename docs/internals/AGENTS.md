# Agent guidance for docs/internals/

## What this directory is

`docs/internals/` contains design notes, implementation strategy, test
philosophy, and a backlog. These files are written primarily by and for
agents working on the codebase. Most users will never open this directory.

## What belongs here

- `design-next.md` — canonical for current landing, recovery, cleanup, and the
  tracking-state model while the specifications remain split.
- `design.md` — authoritative for product behavior outside that scope. Read both
  documents before changing user-visible behavior.
- `implementation-strategy.md` — how the tool is built: component boundaries,
  tooling, test strategy. This file is not a changelog. Update it when the
  *strategy* changes (new tool, new component boundary, new test layer), not
  for every landed implementation change.
- `testing-philosophy.md` — what kinds of tests to write and why.
- `backlog.md` — non-blocking follow-up items: design debt, deferred features,
  open architecture questions. It has no normative authority; add to it rather
  than leaving TODOs in code.

Implementation strategy and evidence-policy documents are subordinate to the product
specifications. An explicitly accepted implementation plan governs its scoped work until its
decisions are folded into the product specification. Critiques, superseded plans, and completed
merger reports are historical evidence, not product specifications.

## Vocabulary

Internal files can use the full implementation vocabulary freely: revsets,
bookmarks, tracking state, operation log, ancestry shape, trunk mapping, ready
prefix, fail-closed, materialized, etc. That vocabulary is appropriate here
because these files describe implementation, not user experience.

Do not carry that vocabulary into `docs/` (the user-facing guides). See
`docs/AGENTS.md` for the boundary.

## When to update these files

- **design-next.md and design.md**: update the authoritative document for the
  affected scope when adding a command, changing semantics, or adding a behavioral
  invariant. Neither document is a changelog.
- **implementation-strategy.md**: update only when the build, test, or
  component strategy changes. `jj log` is the changelog; this file is not.
- **backlog.md**: add items here instead of leaving inline TODOs or comments
  about future work in the code.

## What not to put here

These files are not a changelog, a commit log summary, or a task list for the
current conversation. Use jj commit messages for history and the task tools
for in-conversation tracking.
