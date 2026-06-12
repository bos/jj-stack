# Agent guidance for docs/

## What this directory is

`docs/` is the user-facing documentation set for `jj-review`. These files are
read by real users — people (and agents) who are learning the tool, looking up
a workflow step, or troubleshooting a problem. Write them accordingly.

## Tone and vocabulary

Readers know `jj` and `git`. Standard jj terms are fine: revset, bookmark,
`@-`, `trunk()`, change ID, working copy. Don't over-explain those.

What to avoid is vocabulary that comes from `jj-review`'s own design docs and
implementation — terms a jj user would not know without reading the source:

- Not "ready prefix" → "the changes at the bottom of your stack that are ready"
- Not "ancestry shape" → describe what happened: "your remaining changes are
  still based on the old history"
- Not "materialized locally" → "set up local tracking for"
- When mentioning persisted records, say "tracking data" or describe the
  effect, e.g. "jj-review doesn't know about these PRs yet"
- Not "local-history repair path" → just say what the command does
- Not "remote review branches" → "review branches" is fine; "remote review
  branches" is an internal double-noun
- Not "outstanding incomplete operation" → "failed command" or "interrupted command"

The distinction is between standard jj/git vocabulary (fine) and
`jj-review`-specific design prose that leaked into the wrong layer (not fine).

## What belongs here vs. docs/internals/

**`docs/`** — user-facing guides. These files should explain what to do and
why, not how the tool is built. If a section starts sounding like it is
explaining implementation decisions, move that reasoning to `docs/internals/`.

**`docs/internals/`** — internal notes read primarily by agents and
contributors. Design decisions, implementation strategy, test philosophy,
backlog. These files freely use internal vocabulary and can reference code
structure, data models, and architectural tradeoffs. Most users will never
open this directory.

## Built-in `--help` text

The `--help` output for every command is held to the same standard as these
docs. Command docstrings and flag descriptions live in
`src/jj_stack/commands/*.py` and in `src/jj_stack/cli.py`. Apply the same
vocabulary rules there: standard jj/git terms are fine; `jj-review` internal
design-doc language is not.

Specific patterns to watch for in help text:

- Not "ready prefix" — say "the ready changes at the bottom of the stack"
- Say "readiness checks" or describe the checks directly
- Say "what would be undone" when previewing cleanup or reset behavior
- For persisted records, say "tracking data" or describe the effect
- Say "tracking" rather than naming jj-review's local tracking implementation

## Updating user docs after implementing something

When you add a feature or change behavior, ask:

1. Does `docs/troubleshooting.md` need a new symptom/fix entry?
2. Does `docs/daily-workflow.md` need a new step or a note in "When something
   goes wrong"?
3. Does `docs/README.md` need an updated command list?

The `--help` output is the canonical flag reference. User docs should explain
*when* and *why* to use a command, not duplicate the flag list.
