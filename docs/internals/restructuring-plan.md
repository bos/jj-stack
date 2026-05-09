# Restructuring plan

This document is a strategy doc, not a changelist. It captures the goals of a multi-step
internal restructuring and the order in which we intend to get there. It does not change
any user-visible behavior. The canonical product spec stays
[design.md](./design.md); the implementation strategy stays
[implementation-strategy.md](./implementation-strategy.md). This file describes how the
internals should evolve so that those two docs remain easy to enforce.

## Why restructure

The product is conceptually small: read the `jj` DAG, classify each change against saved
data and GitHub state, decide what to push, push it, write a small amount back. The data
models reflect that — `models/review_state.py` and `models/intent.py` are tens of lines
each. The implementation does not. `src/` is roughly 24k lines and `commands/` alone is
about 14k. The size lives in orchestration: status rendering, mutation ordering, recovery
fan-out, and the per-command repetition of similar predicates over the same handful of
fields.

Three patterns in particular have grown organically and are now load-bearing in ways the
docs did not anticipate:

- The intent-file system is being asked to be many things at once: a concurrency lock, a
  resumption hint, a partial-progress record, and (for `land`) a mutable progress log.
  [design.md](./design.md) explicitly frames intent records as "diagnostic state, not a
  replay script", but the code now leans on them for partial-progress accounting in
  `commands/land/execute.py` and for cross-command retraction in `commands/abort.py`.
- The change lifecycle is implicit. There is no single classifier for "what state is this
  change in"; instead `commands/status.py`, `commands/cleanup.py`, `commands/close.py`,
  `commands/land/plan.py`, and `review/topology.py` each derive a different slice of it
  from the same fields on `CachedChange`, `PullRequestLookup`, and `LocalStack`. The
  largest single example is the landability cascade in `commands/land/plan.py` (around
  130 lines of nested `if/elif` over local conflict, link state, divergence, and PR
  state).
- Parameter signatures have widened to compensate for the missing context objects.
  `bootstrap.AppContext` is constructed once and immediately destructured at the async
  boundary; orchestrators like `commands/submit/command.py:_run_submit_async` and
  `commands/submit/inputs.py:prepare_submit_inputs` then re-thread `(config, jj_client,
  state_store, dry_run, ...)` through long call chains by hand.

These are symptoms of the same underlying problem: the natural seams of the system are
not represented in code, so each command pays the cost of rediscovering them.

## Goals

The restructuring must satisfy these, in priority order:

1. **Preserve every behavioral invariant in [design.md](./design.md).** The `jj` DAG
   stays the source of truth for stack topology. Saved review data stays a sparse
   per-change cache. GitHub pull requests stay derived from the local stack. Ambiguous
   linkage stays fail-closed. Identity continues to follow `change_id`.
2. **Name the change lifecycle once.** Replace per-command derivation of "what state is
   this change in" with a single derived classification that every command consumes. The
   classification is per change, not per stack.
3. **Separate the write-ahead record from the cached observation.** Move "what
   `jj-review` attempted and how far it got" out of the saved-state cache and out of the
   intent files, into an explicit append-only journal. Saved review data continues to be
   the compact snapshot it is today.
4. **Make the natural seams cheap to pass.** Replace ad-hoc parameter bundles with a
   small set of immutable phase contexts that match real boundaries: command bootstrap,
   parsed options, resolved target, mutation run.
5. **Reduce surface area without flattening it.** The aim is fewer concepts, not fewer
   lines for their own sake. We expect a meaningful but bounded shrink in `commands/`
   and `review/`; the irreducible surface (CLI parsing, console rendering, `jj` and
   GitHub adapters) does not shrink.
6. **Stay shippable throughout.** Each step lands as a sequence of small commits that
   keeps `./check.py` green and does not pause feature work. No grand-rewrite branch.

## Non-goals

- Changing any user-visible command, output, or recovery semantics. If a step would
  require that, it belongs in [design.md](./design.md) first.
- Replacing the `jj` DAG, the `git` adapter, or the GitHub client as the underlying
  transports. Their boundaries are right.
- Making the operation journal a source of truth for stack topology. The journal records
  what `jj-review` did. The DAG records what the repository is.
- Forcing tests to shrink proportionally. The test suite is organized by invariant in
  many places; some of it persists regardless of how the orchestration code is shaped.
- Introducing a single "do everything" command class or god context. The phase contexts
  are small and orthogonal on purpose.

## Plan

The four steps below are ordered so that each one makes the next one easier and so that
each step is independently valuable if we stop after it. Step 1 is the keystone: most of
the later wins fall out of it.

### Step 1 — Derived `ReviewChangeStatus` with orthogonal axes

Today, every command re-derives state from `CachedChange`, `PullRequestLookup`,
`LocalStack`, and bookmark observations. We will introduce one derived per-change
classification that other code consumes instead of re-deriving.

The classification is a small product type with orthogonal axes, not one large enum.
Cross-product enumeration is the wrong shape — most combinations never occur in practice
and the diagonal cases are exactly where bugs hide. Concretely the axes we expect to
need, drawn from predicates the code already branches on:

- `local`: `present` / `rewritten` / `divergent` / `orphaned` / `missing`
- `link`: `untracked` / `active` / `unlinked` / `stale` / `ambiguous`
- `remote_branch`: `absent` / `current` / `drifted` / `conflicted` / `untracked`
- `pr`: `none` / `open` / `draft` / `approved` / `changes_requested` / `closed` /
  `merged` / `missing` / `ambiguous`
- `baseline`: `clean` / `commit_changed` / `parent_changed` / `stack_head_changed`

Rules for adding axes:

- Add an axis only when current branching already proves it necessary. The first cut
  should be derived by reading the if-cascades in `commands/land/plan.py`,
  `commands/cleanup.py`, and `commands/close.py` and listing the predicates they switch
  on. Do not invent axes that no command consumes today.
- Each axis value must be derivable from inputs we already have: the local stack walk,
  saved `CachedChange`, `PullRequestLookup`, and the bookmark observations the `jj`
  adapter already returns. If a value would require new I/O, it does not belong on this
  type yet.
- The classifier is the only place that reads the underlying combination. Commands stop
  reading `pr_state`, `link_state`, and `last_submitted_*` directly and read the
  classification instead.

`review/topology.py` is already a proto-version of this for the baseline axis (see
`submitted_state_disagreements`); the new module absorbs it rather than replacing it.

We migrate consumers in order of risk:

1. `status` and `list` first. They are read-only; a wrong classification is at worst a
   rendering bug, not a mutation.
2. `cleanup`'s pruning predicates next. They already operate on combinations of
   `is_unlinked`, `pr_state`, and bookmark presence; the rewrite replaces those tuples
   with axis reads.
3. `submit`, `close`, and `land` planners last. By the time they migrate, the
   classification has been exercised by the read-only paths and the property tests.

We do not generalize beyond what current callers consume. The classifier exists to
remove duplication, not to enumerate every state the system could theoretically
distinguish.

### Step 2 — Phase contexts

With the classifier in place, the natural argument bundles emerge. Replace ad-hoc
parameter threading with four small immutable contexts:

- `CommandContext` — config, `jj` client, state store, repo metadata. Built once at the
  CLI boundary and preserved through the async boundary instead of being destructured.
  Subsumes the role `bootstrap.AppContext` already plays at start-up.
- `<Command>Options` — parsed and validated CLI flags for one command. Decouples the raw
  argparse surface (where today's 15-parameter `submit()` lives in
  `commands/submit/command.py`) from the rest of the implementation.
- `ResolvedTarget` — the unit a command is acting on: selected stack, per-change
  classification from step 1, remote and GitHub repo identity, bookmark observations.
  This is what `prepare_submit_inputs`, `prepare_close_inputs`, and friends produce
  today, just made uniform.
- `MutationRun` — the carrier for `dry_run`, the operation-journal handle (step 3), and
  any progress callbacks. Makes "this is a dry run" a property of the run rather than a
  flag threaded through every helper.

These are not a hierarchy; commands compose the ones they need. Pure planning code takes
`(CommandContext, ResolvedTarget, Options)` and returns a plan. Code that mutates also
takes a `MutationRun`. The async boundary stops being a context-destruction event.

This step is mechanical once step 1 is done. It is intentionally separate so that the
diff is "introduce four context objects and rewire signatures," not entangled with the
classification work.

### Step 3 — Operation journal, piloted on `land`

Replace the conflated intent-file system with an append-only repo-local journal of
operation events. Event kinds, in order of their natural appearance:

- `begin` — operation started, with command, options, and resolved scope
- `planned mutation` — a specific intended mutation about to be attempted (push
  bookmark, create PR, update PR base, retarget PR, close PR, etc.)
- `mutation applied` — that mutation was attempted and observed to succeed, with the
  observed result (PR number, commit ID, etc.)
- `saved state update` — corresponding update to the saved review data
- `completed` / `abandoned` — operation finished

The journal owns the question "what did `jj-review` attempt, and how far did it get?"
The saved review data continues to own "what we know about each change right now." The
`jj` DAG continues to own stack topology. These three sources are not redundant; each
answers a different question.

`land` is the right pilot because it is the only command today that already accumulates
partial-progress fields in a mutable intent record (see
`commands/land/execute.py:_mark_land_intent_completed` around the
`completed_change_ids` update, and the surrounding mutation in
`models/intent.py:LandIntent`). Replacing that with journal events is a like-for-like
swap, not a behavior change. Recovery for `land` becomes "read the journal, replay the
classification, decide what is left."

During the pilot, the other intent kinds (`SubmitIntent`, `CloseIntent`,
`CleanupRebaseIntent`, `CleanupIntent`, `RelinkIntent`, `AbortIntent`) stay exactly as
they are. The journal must prove itself on `land` first.

### Step 4 — Fold remaining intent kinds onto the journal

Once the journal has carried `land` through real recovery scenarios, migrate the other
operations one at a time, in order of how much state they currently entangle: `submit`,
then `close`, then `cleanup-rebase`, then `relink`. `abort` becomes a journal reader
rather than a per-kind dispatch over `isinstance(intent, SubmitIntent)` etc.

After this step:

- There is one record format for all in-flight operations.
- Stale-detection has one rule, not seven.
- Cross-command retraction (today's `_abort_submit` and friends) is expressed in terms
  of replaying journal events backwards, not in terms of reverse-engineering scope from
  an intent's frozen fields.
- `commands/abort.py` shrinks substantially; the cleanup paths in
  `review/intents.py:retire_superseded_intents` collapse into a single staleness rule.

### Step 5 — Parameter and dead-code mop-up

Most parameter pain disappears once steps 2 and 4 land, because the largest threaded
bundles are the ones the new contexts replace. What is left:

- The CLI surface. `commands/submit/command.py:submit` and its peers become thin
  builders that produce `<Command>Options` and `CommandContext`, not the long-form
  parameter list they are today.
- Pass-through helpers that exist only to forward arguments through one extra layer.
  Inline them.
- The intent-kind branching that no longer has a reason to exist after step 4. Delete
  it.

This step is bookkeeping. It is listed last because doing it earlier would mean redoing
it.

## Sequencing and risk

Each step lands as multiple commits that keep `./check.py` green. The expected ordering
of merges is step 1 (small and incremental, one consumer at a time) → step 2 (single
mechanical sweep once consumers exist) → step 3 (`land` pilot in isolation) → step 4
(one command per commit batch) → step 5 (mop-up).

The two highest-risk moments are:

- The first read-only consumer of the new classification. If the axis values are wrong,
  `status` or `list` will render incorrectly. We catch this with the existing status
  integration tests and the property scenarios in `tests/property/`.
- The `land` journal pilot. Recovery is the riskiest behavior in the codebase. We do not
  cut over until the journal-based `land` passes the existing land integration tests
  and the interrupted-submit oracle scenarios under
  [property-testing.md](./property-testing.md).

If a step exposes a behavior question that is not settled by [design.md](./design.md),
the change pauses there until the design doc is updated; this plan does not authorize
behavior changes.

## What we expect to land

Concretely, after all five steps:

- One `ReviewChangeStatus` module replaces ad-hoc state derivation across `status`,
  `list`, `cleanup`, `close`, `submit`, and `land`.
- Four small context types replace the threaded `(config, jj_client, state_store,
  dry_run, ...)` pattern.
- One journal format replaces seven intent kinds. `commands/abort.py` and
  `review/intents.py` shrink correspondingly.
- The `commands/` tree is meaningfully smaller and the per-command files look more alike
  than they do today.

We do not expect the codebase to halve. The CLI, console, completion, formatting, and
adapter layers are all roughly the size they need to be. The win is that the orchestration
layer stops re-deriving the same things and stops carrying the same bundles by hand.

## Open questions deferred to [backlog.md](./backlog.md)

These are real follow-ups that this plan does not commit to. They become much easier
once the journal exists, but are not prerequisites for any of the five steps:

- Whether the journal should grow a heartbeat for liveness, replacing the current
  PID-plus-7-day-staleness rule in `review/intents.py:intent_is_stale`.
- Whether `submit_recovery.py`'s `SubmitStackRelation` and `SubmitTargetRelation`
  classifications should fold into the main `ReviewChangeStatus` axes or stay as a
  separate recovery-time view.
- Whether `cleanup --rebase` belongs as its own command or as a mode of `cleanup` once
  both consume the same classification.
