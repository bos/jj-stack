# jj-stack replacement design: landing, recovery, and state

Status: draft for review. When adopted, this document supersedes the landing, recovery,
cleanup, and state-model portions of `design.md`; sections of `design.md` it does not cover
(selector defaults, stack discovery, submit stack metadata, view/list semantics beyond
caching) carry forward until rewritten. Claims inherited from `design.md` hold only where
restated here.

This spec exists because the previous landing design grew into a durable-transaction protocol
over systems that cannot share a transaction. The replacement theory is deliberately less
ambitious: observation replaces replay, residue replaces atomic cleanup, and one command owns
convergence.

## Theory

The `jj` DAG owns local topology and content. GitHub owns pull-request lifecycle and merge
results. The tool is a projector and a converger:

- `submit` projects the selected local stack onto review branches and pull requests.
- `sync` converges local state with what GitHub and the remote refs actually report.

Recovery is observation. No command needs to know what an earlier interrupted command was
doing; it inspects the current world and computes the remaining work. There are no durable
transaction records, phases, or replay checkpoints, and no command behavior may depend on one.

The landed predicate, used everywhere a command must decide whether a tracked change has
landed:

> A tracked change is landed iff its pull request reports merged and its merge-result commit
> is an ancestor of the current remote trunk.

For transports that preserve commit IDs (direct push, merge-commit method) the merge-result
commit is the change's own commit and any survivor rebase is a no-op. For squash merges the
merge-result commit is new and survivors need rebasing. Design targets the squash case; the
preserved-ID cases fall out as degenerate.

## Safety kernel

Ranked. When guarantees conflict, the higher rule wins.

1. Never lose or silently abandon local work.
2. Never move or delete a remote ref except with an exact expected current target (a lease).
3. Never mutate a pull request without proving its repository and head-branch identity against
   the linkage being acted on.
4. Never guess ambiguous linkage; fail closed before mutation.
5. Never mutate outside the selected stack.

Everything else converges on retry or leaves residue with a reported next step.

Tracking-metadata consistency is deliberately not in the kernel. Metadata is reconstructible
(`checkout` bootstraps from GitHub, `relink` adopts a specific PR), so losing or discarding it
costs convenience, never correctness. No machinery may be added whose only purpose is to keep
metadata transactionally consistent with remote mutations.

## Durable state: identity only

Per tracked change, the state file stores exactly:

- `change_id` (key)
- `bookmark` — the managed review branch name
- `bookmark_ownership` — managed or external
- `pr_number`
- `last_submitted_commit_id` — the submitted baseline

Nothing else. Explicitly excluded, with the replacement mechanism:

- PR lifecycle (state, review decision, draft, mergeability): live observations, fetched when
  needed. Plain `view` shows the stack and linkage; lifecycle requires `--fetch`.
- PR URL: derived from repository and number.
- Parent/stack-head topology pointers: the DAG owns topology.
- Navigation/overview comment IDs: rediscovered per PR by an embedded marker in the comment
  body, then updated in place.
- Pending transaction records of any kind: none exist.

Review branch naming keeps the existing scheme `{prefix}/{slug}-{short_change_id}`. The
change-ID suffix makes managed branches self-identifying: verification and rediscovery match
on the suffix, so a slug that drifted from the current subject is cosmetic, never an identity
question. The saved record remains authoritative for linkage; the suffix is corroboration and
a recovery aid, not a substitute for fail-closed ambiguity handling.

Deleting the state file loses convenience only. Schema changes discard old files without
migration; re-adoption goes through `checkout`/`relink`.

## Commands

### submit

Unchanged role: resolve one selected linear chain, compute managed branches and desired PR
bases, fetch the relevant refs and PRs, verify every destructive update against a lease and
proven identity, push bottom-up, create or update PRs bottom-up. Fail closed on missing or
ambiguous linkage before any mutation. Auto-close protection for stack rewrites keeps the
current ordering protocol.

### sync

The only convergence and recovery command. Algorithm, for the selected stack:

1. Fetch the remote; resolve current trunk.
2. Evaluate the landed predicate for each tracked change on the selected stack, bottom-up;
   the result is a landed prefix.
3. If a landed change's local commit differs from its submitted baseline (unpublished local
   edits), stop and report before any history rewrite; converging would discard those edits.
4. Rebase the first surviving change onto trunk; `jj` rewrites descendants.
5. Resubmit the surviving tracked changes (ordinary `submit` semantics).
6. Retire landed tracking records; forget local managed bookmarks still pointing at landed
   commits; delete cleanup-eligible remote managed branches under lease.
7. Anything uncertain — a moved branch, an ambiguous link, an unexpected PR state — is
   reported with a next step and skipped, never blocking the rest of the convergence.

`sync` is rerunnable and consults no saved operation state. Running it when there is nothing
to do reports that and exits cleanly. It is the documented answer to every interruption:
of `land`, of `sync` itself, of anything.

### land

`land` is: gate, mutate, then converge via the same routine as `sync`.

Gates (unless explicitly bypassed): each planned PR is open, approved, not draft, and — for
the merge transport — mergeable. The plan is a consecutive prefix from the stack bottom;
the scan stops at the first ineligible change with a case-specific report.

Transports:

- **Direct push.** Refresh any stale review branches (idempotent re-push), then move remote
  trunk to the prefix head with one leased push. A protected-branch rejection proves trunk
  did not move and is reported as a hard error with a classified hint. After the push,
  finalize each planned PR bottom-up (retarget base to trunk, confirm GitHub reports it
  merged); finalization is idempotent and equally reachable from `sync`.
- **Merge.** For each planned PR bottom-up, request the GitHub merge with the resolved
  method; stop fail-closed at the first PR GitHub refuses. The accepted prefix then converges
  through the `sync` routine. Method resolution: `--merge-method`, else the repository's
  single enabled method, else stop and ask. A rebase merge is refused for multi-PR prefixes.
  All three methods are supported; squash is the general case.

Interruption at any point — including loss of a merge acknowledgement — is recovered by
observation: the next `land` or `sync` fetches, applies the landed predicate, and converges.

**Message-only intent note.** Before its first non-idempotent remote mutation, `land` may
write a small note (operation, PR numbers) whose sole purpose is messaging: the next command
that observes convergence work can explain it ("the previous land was interrupted after
GitHub accepted #12, #13") instead of changing state silently. The note may influence what
the tool says, never what it does. It is never validated against the world, never gates any
mutation, and is deleted after the next convergence pass. If a proposed change would make
execution read this note, the change is wrong or the note must be removed.

**Lazy convergence.** `land` and `sync` converge the selected stack only. Other tracked
stacks sharing a landed ancestor are untouched; `view`/`list` may cheaply report "ancestor
merged — run `sync`" for them, and their own next command performs the same convergence.

### cleanup

Explicit, idempotent garbage collection; never required for correctness. It deletes only
tool-owned artifacts whose safety is provable at the time of deletion (leases, ownership
checks) and skips anything ambiguous with a report. There is no promise that a successful
land leaves zero artifacts.

### Repair surface

`checkout` (bootstrap tracking from GitHub), `relink` (adopt one PR for one change),
`restart`/`unlink` (abandon tracking) remain distinct explicit intents with explicit
selectors. They are the answer to every state-file question, including discarded schemas.

## Explicitly unsupported

- Replaying or resuming an interrupted command from saved operation state.
- Automatic reconciliation of non-selected stacks after a land.
- Zero-residue guarantees; a leftover tool-owned branch or bookmark is a report, not a fault.
- State-file migration; old schemas are discarded, then re-adopted.
- Inferring intent from manually rewritten managed branches, replaced PRs, or corrupted
  tracking; these require explicit re-adoption or reset.

## Validation gates

Every implementation slice must answer, in its commit body or review:

1. What user-visible guarantee is removed?
2. What exact safe recovery replaces it?
3. Which destructive failure remains protected?
4. What is the net line change? Slices that centralize rather than delete do not qualify.

The suite keeps proving: no local work lost, no unrelated ref moved, no unrelated PR mutated,
interrupted operations converge through `sync`, ambiguity stops before mutation.

## Adoption

Phased, in the `rework` workspace, each phase check-green:

1. Unwind merge-land reconciliation and all four pending-state models; `land --via merge`
   merges a verified prefix and converges via the `sync` routine.
2. Reduce the state model to identity-only; move comment identity to marker rediscovery.
3. Rebuild direct-push land on the shared gate/mutate/converge shape.
4. Reset tests against this spec (delete transaction-protocol families; keep the safety
   kernel and ordinary `jj`-workflow coverage) and rewrite `design.md` to match, updating the
   `AGENTS.md` canonical-spec pointer at the same time.
