# Agent guidance for docs/internals/

## What this directory is

`docs/internals/` contains design notes, implementation strategy, and test philosophy. These files
are written for contributors and maintainers. Most users will never open this directory.

## What belongs here

- `design.md` — the single canonical product spec and behavioral policy. Read it before changing
  user-visible behavior.
- `implementation-strategy.md` — how the tool is built: component boundaries,
  tooling, test strategy. This file is not a changelog. Update it when the
  *strategy* changes (new tool, new component boundary, new test layer), not
  for every landed implementation change.
- `testing-philosophy.md` — what kinds of tests to write and why.
Only `design.md` defines product behavior. Other documents explain implementation, testing,
or review practice. Accepted plans guide their scoped work but do not change behavior until
`design.md` is updated.

## Vocabulary

Internal files may use standard `jj`, Git, GitHub, and software terms. Project-specific terms are
appropriate only when they name a real type, field, module, or enduring rule. Define them at first
use and prefer concrete inputs, checks, and effects over a new taxonomy.

Active design and strategy documents describe the current system. Do not make readers learn names
for abandoned mechanisms or completed slices; keep that history in `jj` commits. Do not use
internal terminology as permission to make user-facing docs or help harder to understand. See
`docs/AGENTS.md` for the stricter public vocabulary rules.

## When to update these files

- **design.md**: update the relevant section when adding a command, changing semantics, or adding
  a behavioral invariant. It is not a changelog.
- **implementation-strategy.md**: update only when the build, test, or
  component strategy changes. `jj log` is the changelog; this file is not.
## What not to put here

These files are not a changelog, a commit log summary, or a task list for the
current conversation. Use jj commit messages for history and the task tools
for in-conversation tracking.
