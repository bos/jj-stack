# Agent guidance for docs/internals/

## What this directory is

`docs/internals/` contains design notes, implementation strategy, test
philosophy, and a backlog. These files are written primarily by and for
agents working on the codebase. Most users will never open this directory.

## What belongs here

- `design.md` — the single canonical product spec and behavioral policy. Read it before changing
  user-visible behavior.
- `implementation-strategy.md` — how the tool is built: component boundaries,
  tooling, test strategy. This file is not a changelog. Update it when the
  *strategy* changes (new tool, new component boundary, new test layer), not
  for every landed implementation change.
- `testing-philosophy.md` — what kinds of tests to write and why.
- `backlog.md` — non-blocking follow-up items: design debt, deferred features,
  open architecture questions. It has no normative authority; add to it rather
  than leaving TODOs in code.

Only `design.md` defines product behavior. Other documents explain implementation, testing,
review practice, or deferred work. Accepted plans guide their scoped work but do not change
behavior until `design.md` is updated. Completed reports and superseded plans are historical
evidence only.

## Vocabulary

Internal files may use standard `jj`, Git, GitHub, and software terms. Project-specific terms are
appropriate only when they name a real type, field, module, or enduring rule. Define them at first
use and prefer concrete inputs, checks, and effects over a new taxonomy.

Active design and strategy documents describe the current system. Do not make readers learn names
for abandoned mechanisms or completed slices; keep that history in an explicitly historical
report or `jj` commits. Do not use internal terminology as permission to make user-facing docs or
help harder to understand. See `docs/AGENTS.md` for the stricter public vocabulary rules.

## When to update these files

- **design.md**: update the relevant section when adding a command, changing semantics, or adding
  a behavioral invariant. It is not a changelog.
- **implementation-strategy.md**: update only when the build, test, or
  component strategy changes. `jj log` is the changelog; this file is not.
- **backlog.md**: add items here instead of leaving inline TODOs or comments
  about future work in the code.

## What not to put here

These files are not a changelog, a commit log summary, or a task list for the
current conversation. Use jj commit messages for history and the task tools
for in-conversation tracking.
