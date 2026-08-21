# Agent guidance for docs/internals/

## What this directory is

`docs/internals/` contains design notes, implementation strategy, and test philosophy. These files
are written for contributors and maintainers. Most users will never open this directory.

## What belongs here

- `design.md` — enduring product rules, not an exhaustive catalog of observable behavior.
- `implementation-strategy.md` — component boundaries, tooling, and test strategy. It is not a
  changelog.
- `testing-philosophy.md` — what kinds of tests to write and why.

## Vocabulary

Internal files may use standard `jj`, Git, GitHub, and software terms. Project-specific terms are
appropriate only when they name a real type, field, module, or enduring rule. Define them at first
use and prefer concrete inputs, checks, and effects over a new taxonomy.

Active design and strategy documents describe the current system. Do not make readers learn names
for abandoned mechanisms or completed slices; keep that history in `jj` commits. Do not use
internal terminology as permission to make user-facing docs or help harder to understand. See
`docs/AGENTS.md` for the stricter public vocabulary rules.

## What not to put here

These files are not a changelog, a commit log summary, or a task list for the
current conversation. Use jj commit messages for history and the task tools
for in-conversation tracking.
