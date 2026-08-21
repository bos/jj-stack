# Agent guidance for docs/

## What this directory is

`docs/` is the user-facing documentation set for `jj-stack`. These files are
read by real users — people (and agents) who are learning the tool, looking up
a workflow step, or troubleshooting a problem. Write them accordingly.

## Tone and vocabulary

Readers know `jj` and `git`. Standard jj terms are fine: revset, bookmark,
`@-`, `trunk()`, change ID, working copy. Don't over-explain those.

Use the product nouns precisely:

- A **pull request** or **PR** is the GitHub object.
- A **PR branch** is a git branch intended to be a PR head.
- A **stack** is an ordered chain of local changes or the corresponding GitHub grouping.
- **Review** means human review activity: comments, approvals, requested changes, reviewers,
  and review feedback. Do not use it as a synonym for a PR, PR branch, or stack.

What to avoid is vocabulary that comes from `jj-stack`'s own design docs and
implementation — terms a jj user would not know without reading the source:

- Not "ready prefix" → "the changes at the bottom of your stack that are ready"
- Not "ancestry shape" → describe what happened: "your remaining changes are
  still based on the old history"
- Not "materialized locally" → "set up local tracking for"
- When mentioning persisted records, say "tracking data" or describe the
  effect, e.g. "jj-stack doesn't know about these PRs yet"
- Not "local-history repair path" → just say what the command does
- Not "remote PR branches" → "PR branches" is fine
- Not "outstanding incomplete operation" → "failed command" or "interrupted command"

The distinction is between standard jj/git vocabulary (fine) and
`jj-stack`-specific design prose that leaked into the wrong layer (not fine).

## What belongs here vs. docs/internals/

**`docs/`** — user-facing guides. These files should explain what to do and
why, not how the tool is built. If a section starts sounding like it is
explaining implementation decisions, move that reasoning to `docs/internals/`.

**`docs/internals/`** — internal notes read primarily by agents and
contributors. Design decisions, implementation strategy, and test philosophy.
These files freely use internal vocabulary and can reference code structure,
data models, and architectural tradeoffs. Most users will never open this
directory.

## Built-in `--help` text

The `--help` output for every command is held to the same standard as these
docs. Command docstrings and flag descriptions live in
`src/jj_stack/commands/*.py` and in `src/jj_stack/cli.py`. Apply the same
vocabulary rules there: standard jj/git terms are fine; `jj-stack` internal
design-doc language is not.

Specific patterns to watch for in help text:

- Not "ready prefix" — say "the ready changes at the bottom of the stack"
- Say "readiness checks" or describe the checks directly
- Say "what would be undone" when previewing cleanup or reset behavior
- For persisted records, say "tracking data" or describe the effect
- Say "tracking" rather than naming jj-stack's local tracking implementation

## Routing user documentation changes

When the root documentation gate is met, update only the closest relevant location:

- Add to `docs/troubleshooting.md` only for a recurring user-visible symptom and recovery.
- Change a guide under `docs/guides/` only when its workflow steps or decisions change.
- Change `docs/README.md` only when the documentation overview or command set changes.

The `--help` output is the canonical flag reference. User docs should explain
*when* and *why* to use a command, not duplicate the flag list.
