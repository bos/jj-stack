# Restructuring plan

A multi-step internal restructuring to make the change lifecycle explicit, replace the
conflated intent-file system with an operation journal, and tighten parameter threading.

[design.md](./design.md) remains canonical ([AGENTS.md](./AGENTS.md)). Where this plan
would change documented behavior, the process is: name the change here, decide it in
[design.md](./design.md) first as part of the same commit batch, then implement against
the new design. Where [design.md](./design.md) has silently drifted from the code, the
same process applies — the disagreement is named, decided, and the code follows.

## Why restructure

The product is conceptually small: read the `jj` DAG, classify each change against
saved data and GitHub state, decide what to push, push it, write a small amount back.
`models/review_state.py` and `models/intent.py` are tens of lines each. But `src/` is
roughly 24k lines and `commands/` alone is about 14k. The size lives in orchestration:
status rendering, mutation ordering, recovery fan-out, and per-command repetition of
similar predicates over the same fields.

Three patterns have grown organically and are now load-bearing:

- The intent-file system is being asked to be many things at once: a concurrency lock,
  a resumption hint, a partial-progress record, and (for `land`) a mutable progress
  log. [design.md](./design.md) frames intent records as "diagnostic state, not a
  replay script", but the code now leans on them for partial-progress accounting and
  cross-command retraction.
- The change lifecycle is implicit. `commands/status.py`, `commands/cleanup.py`,
  `commands/close.py`, `commands/land/plan.py`, and `review/topology.py` each derive a
  different slice from the same fields on `CachedChange`, `PullRequestLookup`, and
  `LocalStack`. The 130-line landability cascade in `commands/land/plan.py` is the
  largest single example.
- Parameter signatures have widened. `bootstrap.AppContext` is constructed once and
  immediately destructured at the async boundary; orchestrators re-thread `(config,
  jj_client, state_store, dry_run, ...)` through long call chains by hand.

## Goals

In priority order:

1. **Preserve the core invariants.** The `jj` DAG is the source of truth for stack
   topology. Saved review data is a sparse per-change cache. PRs are derived from the
   local stack. Ambiguous linkage fails closed. Identity follows `change_id`. Other
   parts of [design.md](./design.md) are revisited only via the named-change process
   above.
2. **Name the change lifecycle once.** A single derived per-change classifier replaces
   per-command derivation.
3. **Separate write-ahead record from cached observation.** An explicit append-only
   journal owns "what `jj-review` attempted and how far it got." Saved review data
   stays a sparse snapshot. Journal entries are retained after completion so post-hoc
   debugging ("how did `state.json` come to record this PR as merged?") is answerable
   from on-disk artifacts.
4. **Make natural seams cheap to pass.** Replace ad-hoc parameter bundles with a small
   set of immutable phase contexts.
5. **Reduce surface area without flattening it.** Fewer concepts, not fewer lines for
   their own sake. CLI parsing, console rendering, and the `jj` / GitHub adapters do
   not shrink.
6. **Stay shippable throughout.** Each step lands as small commits that keep
   `./check.py` green. No grand-rewrite branch.

## Non-goals

- Replacing the `jj` DAG, `git` adapter, or GitHub client. Their boundaries are right.
- Making the journal a source of truth for stack topology.
- Forcing tests to shrink proportionally — many are organized by invariant.
- A single "do everything" command class or god context.

## Plan

Five top-level steps, each independently valuable.

For each step:
1. Design and implement the change. Update the design docs accordingly.
2. Review it thoroughly using a subagent. Fix the issues found. Iterate until none remain.
3. Make self-contained commits.

Step 1 is the keystone.

### Step 1 — Derived `ReviewChangeStatus` with orthogonal axes

Today every command re-derives state from `CachedChange`, `PullRequestLookup`,
`LocalStack`, and bookmark observations. Replace with one derived per-change classifier
that other code consumes.

Orthogonal axes, not one large enum — cross-product enumeration is the wrong shape and
the diagonal cases are where bugs hide:

- `local`: `present` / `divergent` / `orphaned` / `missing`. "Rewritten" lives on
  `baseline.commit_changed`; an amended change is still locally `present`.
- `link`: `untracked` / `active` / `unlinked`. Saved-state truth only
  (`models/review_state.py:LinkState`). PR-lookup ambiguity is independent and lives
  on the PR axes.
- `remote_branch`: `absent` / `current` / `drifted` / `conflicted` / `untracked`.
- `remote_branch_matches_commit`: `bool | None`. Only meaningful for a single observed
  remote target; lets submit preserve the distinction between an untracked branch that
  is already at the desired commit and one that must be updated through the fallback
  Git remote path.
- `pr_lifecycle`: `none` / `open` / `closed` / `merged` / `missing` / `ambiguous`.
- `pr_draft`: `bool | None`. Only meaningful when `pr_lifecycle == open`.
- `pr_review_decision`: `none` / `approved` / `changes_requested` / `commented` /
  `unknown`. Only meaningful when `pr_lifecycle == open`. The PR-axis split is forced
  by `commands/land/plan.py`, which switches on lifecycle, draft, and review decision
  independently — a draft PR can be approved.
- `baseline`: a flag set, not a single value.
  `review/topology.py:SubmittedStateDisagreement` reports `commit_changed`,
  `parent_changed`, `stack_head_changed` independently. `stack_head_changed` is a
  stack-level fact and migrates to a stack view.

Rules:

- Add an axis only when current branching proves it necessary. Read the if-cascades
  in `commands/land/plan.py`, `commands/cleanup.py`, `commands/close.py` first.
- Each axis must be derivable from inputs already available; no new I/O.
- The classifier is the only place that *branches* on raw combinations. Mutation and
  persistence code keeps reading and writing the underlying fields directly — that is
  what populates the classifier.
- `ReviewChangeStatus` is observational only. It has no mutation methods and no
  command policy. Commands consume it through small policy helpers such as
  "landability" or "cleanup eligibility"; classification itself does not decide what
  to do.

`review/topology.py` is a proto-version of the baseline axis; the new module absorbs
it.

Migration order, by risk:

1. Prototype on `list`, then `doctor`. Both verified pure reads (no
   `state_store.save`, no intent writes). If this makes those commands harder to read,
   fix the abstraction before expanding it.
2. `status`. *Not* read-only — see Sequencing and risk for mitigations.
3. `cleanup`'s pruning predicates.
4. `submit`, `close`, `land` planners.

Step 1 succeeds when those consumers no longer branch directly on raw
`CachedChange` / `PullRequestLookup` combinations, and the classifier has unit tests
for the representative states each migrated command relies on.

### Step 2 — Phase contexts

Four small immutable contexts replace ad-hoc parameter bundles:

- `CommandContext` — config, `jj` client, state store, repo metadata. Built once at
  the CLI boundary and preserved through the async boundary instead of being
  destructured.
- `<Command>Options` — parsed CLI flags for one command. Decouples the raw argparse
  surface (today's 15-parameter `submit()`) from the rest.
- `ResolvedTarget` — selected stack, per-change classification, remote and GitHub repo
  identity, bookmark observations. What `prepare_submit_inputs` /
  `prepare_close_inputs` produce, made uniform.
- `MutationRun` — `dry_run`, journal handle, progress callbacks. Makes "this is a dry
  run" a property of the run.

Pure planning code takes `(CommandContext, ResolvedTarget, Options)`. Mutating code
also takes `MutationRun`. Mechanical once step 1 is done; kept separate so the diff is
just signature rewiring.

Step 2 succeeds when the wide orchestration signatures shrink materially without hiding
dependencies in a god object. After this step, pause before starting Step 3 and ask:
did the classifier and contexts make command code clearer, and are the remaining
intent/recovery problems still large enough to justify the journal migration? If not,
revise this plan before touching recovery machinery.

### Step 3 — Concurrency lock and operation journal

Three sub-phases that land in order. Conflating them is the mistake the previous
version of this plan made.

#### Phase 3a — Single-writer concurrency primitive

Today, intent files double as same-kind concurrency mutexes
(`state/intents.py:check_same_kind_intent`); `AbortIntent` is written purely as a
lock. None of this is atomic acquisition — `state/intents.py:_intent_filename`
deliberately allocates *unique* names, the opposite of what a lock needs.

Replace with an exclusive advisory lock on a fixed-path sentinel file in the repo
state directory, held for the operation's lifetime, with a non-blocking try-acquire
variant. The primitive is cross-platform; `system.py` already has the Windows
branch precedent. A companion file records `(command, PID, start time,
journal/intent path)` for diagnostics; stale companions with dead PIDs are cleaned
up by the next acquirer.

Three tiers of lock interaction:

- **Pure readers** — `list`, `doctor`. Do not touch the lock.
- **Reader with best-effort cache write** — `status`. Non-blocking `try_lock` around
  the cache write only; if held, skips the cache update and reports "cache not
  refreshed — another `jj-review` operation is running." Live status data still
  renders. Today this write races silently.
- **Mutators** — `submit`, `close`, `cleanup`, `cleanup --rebase`, `land`, `restart`,
  `relink`, `unlink`, `abort`, `import`. Take the lock blocking with a short timeout;
  on timeout, fail naming the holder. No auto-kill. `import` joining the lock-takers
  is new behavior (it has no intent file today and is currently unsynchronized) but
  introduces no recovery semantics — `import_.py:715` is a single atomic save with no
  mid-state.

Per-kind intent files keep working unchanged in 3a; same-kind live waiting in
`check_same_kind_intent` becomes unnecessary under the lock. Stale-intent discovery
and reporting still run until those intents migrate in step 4.

#### Phase 3b — Operation journal pilot on `land`

Append-only journal events:

- `begin` — operation, options, resolved scope, lock holder.
- `planned mutation` — about to attempt push / create-PR / retarget / close-PR / etc.
- `mutation applied` — succeeded, with the GitHub response payload that justified
  treating it as success.
- `saved state update` — `CachedChange` before/after delta.
- `completed` / `abandoned`.

`land` is the right pilot because it is the only command that already accumulates
partial-progress fields in a mutable intent
(`commands/land/execute.py:_mark_land_intent_completed`). The pilot replaces
`LandIntent`'s in-place mutation with journal events; other intent kinds stay
untouched.

The journal's first job is replacing in-flight intent/progress records. Durable audit
value is secondary and must not expand the event schema beyond recovery needs in the
first implementation. Once Step 3 begins, detailed lock APIs, event schemas, and
retention mechanics belong in [implementation-strategy.md](./implementation-strategy.md)
or a focused journal design doc; this file stays the roadmap.

Replay fold for `land` recovery, specified before cutover:

- `planned mutation` without matching `mutation applied` → re-attempt. The GitHub
  side must be idempotent on retry (no-op response treated as success).
- `mutation applied` without following `saved state update` → derive the cache delta
  from the applied event's payload and emit the missing update.
- The latest `landed_change_ids` prefix is folded from `mutation applied` events;
  resume proceeds only if the result matches the live DAG's classification.
- Subsequent invocations finding a dead-PID lock holder write `abandoned` at acquire
  time.

##### Retention for post-hoc debugging

Journal entries are useful for post-hoc debugging only if they survive the operation
that wrote them:

- Entries are not deleted on `completed`; they are marked completed and retained.
- Bounded retention (~50 ops or ~30 days, whichever is larger). Pruning at the next
  operation, not in the foreground.
- `mutation applied` events store the full GitHub response payload locally. PR
  titles, bodies, and commit messages are user content; redaction policy is "do not
  surface in any output that leaves the repo."
- A read-side observation log (`status` recording GitHub state outside any operation)
  is *not* in scope here — it would re-couple readers to the lock and balloon volume.
  Deferred.

#### Phase 3c — Retire `land`'s old intent code

After 3a and 3b have shipped and the journal-based `land` passes the land-specific
recovery scenarios, remove `LandIntent`, its in-place mutation, and `land`'s
`check_same_kind_intent` call sites. Other intent kinds stay; they migrate in step 4.

Step 3 succeeds when `LandIntent` is gone, interrupted-land behavior is unchanged, and
the new lock has cross-process coverage for timeout, dead-PID recovery, `try_lock`, and
holder diagnostics.

### Step 4 — Fold remaining intent kinds onto the journal

Migrate the remaining intent-backed paths onto the journal: `submit`
(`SubmitIntent`); `close` and `close-orphan` (both `CloseIntent`); `cleanup` and
`cleanup --rebase` (`CleanupIntent` and `CleanupRebaseIntent`); `relink`
(`RelinkIntent`). `AbortIntent` is removed entirely — its lock-sentinel role is
subsumed by 3a. `import` is a special case (below). `abort` becomes a journal
reader dispatching to per-command policy.

Per-command policy is preserved verbatim:

- **`submit`** — retracts pushed branches and PRs when recorded scope still matches
  the live DAG; refuses retraction otherwise. Today's `commands/abort.py:_abort_submit`.
- **`land`, `close`, `cleanup --rebase`, `relink`** — remove the journal entry,
  report a diagnostic. No partial work reversed.
- **`import`** — single atomic save, no mid-state. Records a journal entry for
  forensic continuity; `abort` removes the entry.

Any expansion of `abort`'s reach (partial-land retraction, close reversal) stays in
[backlog.md](./backlog.md).

#### Two stale-detection policies, kept distinct

`review/intents.py` has two separate concerns that must not collapse:

- **Liveness / TTL** (`intent_is_stale`) — "holder PID dead AND change-ids no longer
  resolve, or — for `Cleanup` / `Relink` — entry > 7 days old." With dead-PID
  detection at lock-acquire time, the 7-day fallback may become unnecessary.
- **Supersession** (`retire_superseded_intents`) — content-aware: "this newer
  operation's recorded scope covers the older entry's." Cannot reduce to "saw a newer
  `completed` event."

Both become uniform across kinds; both stay distinct policies.

Step 4 succeeds when `models/intent.py` no longer has per-command intent kinds,
`abort` dispatches through one journal-backed table, and the submit/close/cleanup/relink
recovery policies match their pre-journal behavior.

### Step 5 — Parameter and dead-code mop-up

After steps 2 and 4:

- CLI surface — `commands/submit/command.py:submit` and peers become thin builders
  for `<Command>Options` and `CommandContext`.
- Pass-through helpers that exist only to forward arguments — inline.
- Intent-kind branching with no remaining reason to exist — delete.

## Sequencing and risk

Order of merges: 1 → 2 → 3a → 3b → 3c → 4 (one command per batch) → 5.

Riskiest moments:

- **The `status` migration** (step 1). `commands/status.py:prepare_status` defaults
  `persist_cache_updates=True`, reaching `state_store.save` via
  `review/status.py:_persist_status_cache_updates`. A wrong classifier corrupts saved
  fields. Pilot on `list` and `doctor` first; pin the refresh contract with an
  integration test; run new and old derivations in parallel for at least one release.
- **The lock primitive** (step 3a). `tests/unit/test_concurrency.py` and
  `test_pytest_concurrency.py` cover *in-process* async and xdist worker analysis,
  not file locking. Step 3a must add cross-process tests covering blocking
  acquisition with timeout, dead-PID recovery, the `try_lock` path, and the
  holder-file diagnostic before later steps depend on the lock. Tests cover both
  POSIX and Windows.
- **The `land` journal pilot** (step 3b). `land` recovery is targeted intent-keyed
  exact resume that depends on `landed_commit_id` — *not* the same shape as
  interrupted-`submit` recovery (rerun-and-converge). Submit oracles are the wrong
  test bar. Required: existing land integration suite, including
  resume-after-trunk-already-moved at `tests/integration/test_land_command.py`; new
  scenarios for failed trunk push, trunk pushed but PR retargets interrupted, partial
  `landed_change_ids` prefix, crash between a `mutation applied` and its paired
  `saved state update`, abort in each mid-state.

## Behavior changes named in this plan

Each is decided in [design.md](./design.md) first, then implemented.

- **Cross-command concurrency** (3a) — mutators serialize against each other, not
  just same-kind invocations.
- **`import` joins the lock-takers** (3a) — unsynchronized today.
- **`status` cache-write under contention** (3a) — `try_lock`; skips with a
  diagnostic when held. Today it races silently.
- **Journal retention** (3b) — completed entries retained for forensic reconstruction,
  bounded by size/age.
- **Liveness and supersession rules become uniform** (4) — across all kinds; the two
  policies stay distinct.
- **`abort` dispatch shape** (4) — one dispatch table over the journal. Per-command
  policies preserved verbatim.

Anything not listed here is preserved.

## Open questions deferred to [backlog.md](./backlog.md)

- Whether the journal needs a heartbeat once dead-PID detection happens at
  lock-acquire time.
- Whether `submit_recovery.py`'s `SubmitStackRelation` and `SubmitTargetRelation`
  fold into `ReviewChangeStatus` or stay recovery-time-only.
- Whether `cleanup --rebase` is its own command or a mode of `cleanup` once both
  consume the same classification.
- A read-side observation log: high-frequency, small-payload, lock-incompatible by
  design. Separate audit-log feature later.
- Whether `import` should record a journal entry given it has no mid-state —
  forensic continuity vs. noise.
