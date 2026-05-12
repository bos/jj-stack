# Agent guidance for docs/internals/

## What this directory is

`docs/internals/` contains design notes, implementation strategy, test
philosophy, and a backlog. These files are written primarily by and for
agents working on the codebase. Most users will never open this directory.

## What belongs here

- `design.md` — canonical product spec and behavioral policy. Read this before
  changing any user-visible behavior. It is the source of truth for what the
  tool is supposed to do.
- `implementation-strategy.md` — how the tool is built: component boundaries,
  tooling, test strategy. This file is not a changelog. Update it when the
  *strategy* changes (new tool, new component boundary, new test layer), not
  for every landed implementation change.
- `testing-philosophy.md` — what kinds of tests to write and why.
- `backlog.md` — non-blocking follow-up items: design debt, deferred features,
  open architecture questions. Add to it rather than leaving TODOs in code.

## Vocabulary

Internal files can use the full implementation vocabulary freely: revsets,
bookmarks, tracking state, operation log, ancestry shape, trunk mapping, ready
prefix, fail-closed, materialized, etc. That vocabulary is appropriate here
because these files describe implementation, not user experience.

Do not carry that vocabulary into `docs/` (the user-facing guides). See
`docs/AGENTS.md` for the boundary.

## When to update these files

- **design.md**: when adding a new command, changing existing command
  semantics, or adding new behavioral invariants. The design doc is not a
  changelog — update the relevant sections to reflect current behavior.
- **implementation-strategy.md**: update only when the build, test, or
  component strategy changes. `jj log` is the changelog; this file is not.
- **backlog.md**: add items here instead of leaving inline TODOs or comments
  about future work in the code.

## What not to put here

These files are not a changelog, a commit log summary, or a task list for the
current conversation. Use jj commit messages for history and the task tools
for in-conversation tracking.
