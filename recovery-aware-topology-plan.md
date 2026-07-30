# Recovery-aware topology project plan

> This is a temporary execution plan. It is authoritative for project scope, sequencing,
> ownership, and acceptance, but it is not a product specification.
> [design.md](docs/internals/design.md) remains the only authority for product behavior.
> Delete this file after the final project audit is accepted.

## Authority and lifecycle

This plan contains only work that remains or is in flight. A completed slice is deleted from this
file in the change that implements it. The implementing diff therefore shows both the promised
work and its removal, and the independent reviewer decides whether that removal is justified.
`jj` history records completed work; this file does not become a changelog.

Behavioral decisions are written into
[design.md](docs/internals/design.md) before or with the code that implements them.
[implementation-strategy.md](docs/internals/implementation-strategy.md) describes only
architecture that exists after the implementing change. User documentation and help change with
the behavior they explain.

The current complexity budgets are telemetry during this project, not design constraints. Every
slice records relevant measurements and runs `./check.py`, but a temporary budget failure does not
force an artificial boundary or an incomplete replacement. Any final budget revision is an
explicit final-review decision supported by the completed design and before/after evidence.

## Coordination and review protocol

The primary agent is the coordinator. It owns this plan, dependency ordering, exact revision
handoffs, `jj` integration, and go/no-go decisions. It does not implement or review its own code
slices.

Each implementation slice follows this protocol:

1. The coordinator records the accepted parent change ID and exact commit ID.
2. One implementation agent receives a bounded brief naming this plan section and the canonical
   documents it must read.
3. Isolated parallel work uses a sibling `jj workspace`, never a Git worktree. Only disjoint
   adopters run in parallel after the pure model interface is accepted.
4. The implementer reports the change ID, exact commit ID, tests, deletions, documentation
   changes, and unresolved risks.
5. A different agent reviews that exact commit against
   [design.md](docs/internals/design.md),
   [testing-philosophy.md](docs/internals/testing-philosophy.md), and
   [code-reviews.md](docs/internals/code-reviews.md).
6. Findings return to the implementer. Any changed commit ID receives another review.
7. The coordinator runs or verifies focused tests and `./check.py`, checks the promised deletions,
   and accepts or rejects the slice.

Agents receive this plan, the files named by their slice, and concise predecessor reports. They do
not inherit the full project conversation. A reviewer reads the frozen artifact and canonical
documents rather than relying on an implementer's summary.

The plan section for a slice is deleted only in the exact implementation commit accepted by the
reviewer. The project-wide success criteria and final-audit sections remain until the project
ends.

## Project-wide success criteria

### One authority for local review topology

- One pure model derives complete local review paths, copies sharing a change ID, tracked
  placement, path overlap, and local dependencies.
- Selected and repository-wide projections use the same rules over different complete snapshots.
- Paths include untracked ancestors that affect review ordering.
- Immutable trunk copies annotate local recovery state and never become independent local stacks.
- Revision, change-ID, and PR selectors resolve through the same selection rules.
- Every lifecycle command consumes the shared result rather than reconstructing topology.
- All superseded discovery, selection, orphan, stale, and dependency authorities are deleted.
- No durable topology, replay, transaction, or alias state is added.

The required consumers are `list`, `view`, `submit`, `merge`, selected `sync`, `sync --all`,
`unstack` including orphan modes, `cleanup`, `checkout`, `relink`, and topology diagnostics in
`doctor`.

### A pure and testable model

The model accepts immutable typed observations and returns immutable typed projections. It
cannot reach a subprocess, `jj` or Git client, filesystem path, state store, GitHub client,
configuration, clock, callback, lazy query, global state, or mutation API.

Adapters collect and batch external facts before calling the model. Commands apply GitHub
evidence, identity safety, native-stack policy, cleanup eligibility, and mutation ordering after
receiving the model result.

Provisional implementation names may include a snapshot, revision observation, change copies,
review path, tracked placement, and selection problem. These names are not accepted terminology
until the implementing review shows that each names an enduring type or rule. Prefer ordinary
`jj`, Git, and GitHub language over a new taxonomy.

The pure model satisfies these laws:

- Input ordering cannot change the semantic result.
- Every in-scope revision is accounted for as local path content, trunk context, a boundary, or a
  typed unsupported condition.
- Every returned path is parent-connected, complete to its declared boundary, and preserves local
  ancestor order.
- Tracking annotates topology but does not create it.
- One mutable local copy plus an immutable trunk copy is a recoverable shape; more than one
  mutable copy is ambiguous.
- Adding a competing mutable copy cannot turn an ambiguous selection into a unique one.
- A local commit differing from its submitted baseline remains unpublished regardless of patch or
  tree equivalence.
- Selected and repository projections agree about every shared revision and tracked placement.
- Reachable observations return a result or a typed problem rather than crashing.

### Consistent and safe behavior

- `list` and `view` show the same complete path that mutating commands would use.
- A normal external ordinary or native rebase merge is recoverable by change-ID and PR selectors.
- Squash recovery continues to work without a preserved change ID.
- Truly ambiguous local copies fail closed without mutation.
- Unpublished local work or ordering is never discarded implicitly.
- Accepting GitHub's landed ordering after an unpublished local rewrite requires explicit user
  authority.
- Orphan and cleanup classification agree across every command that exposes or mutates them.
- Every stop identifies the relevant local, submitted, and trunk commits when they differ and
  gives a runnable next step.
- Legacy local review bookmarks are diagnosed from saved review identities, not from a permanent
  legacy-prefix policy or migration mechanism.

### Replacement, not accumulation

The finished project contains none of these competing authorities:

- `JjClient.discover_review_stack` and its stack-boundary policy;
- `allow_divergent` and `allow_immutable` policy combinations;
- `discover_tracked_stacks`, `discover_connected_tracked_stacks`, and
  `discover_stacks_from_revisions`;
- change-ID-tuple path deduplication;
- PR selection's separate visible-copy and path-membership policy;
- cleanup's separate local stale-topology classifier;
- command-local stack pickers, orphan placement, or descendant-path decisions that the model can
  answer;
- compatibility wrappers preserving an old and new policy path;
- tests and helpers whose only purpose was a deleted authority.

## Decisions required before behavioral implementation

These decisions must be accepted in `design.md` before the selected-topology replacement lands:

1. **Selector rule.** A change ID or linked PR selects the unique mutable off-trunk copy when an
   immutable trunk copy also exists. More than one mutable copy fails closed. Trunk-only tracking
   is a typed nonlocal condition handled only by commands whose documented scope permits it.
2. **Inventory overlap.** Repository inventory reports complete maximal off-trunk paths. Shared
   prefixes may appear in each rendered path but share one internal commit identity. Trunk copies
   annotate paths and never form rows.
3. **Inspection completeness.** Local shape alone does not prove a merge. Inspection classifies
   the shape after GitHub lookup; proven merged recovery is cleanup work, while unmerged
   divergence remains incomplete.
4. **Explicit remote-order acceptance.** Decide the flag or command wording, confirmation
   semantics, and dry-run behavior. Any name in this plan is provisional.
5. **Ordering effect.** In the reported case, accepting GitHub history means the merged change is
   already on trunk, followed by the formerly lower unreviewed change and then the remaining local
   descendants. The UX must state this reorder before applying it.
6. **Observation scope.** A selected snapshot contains the complete connected component needed
   for selection and dependency checks. A repository snapshot contains all visible off-trunk
   candidates plus every visible copy of tracked change IDs, without scanning all history.

## Test strategy and acceptance

Before adding tests, each implementation agent inventories existing coverage for the policy
being replaced. Every proposed test names the user-reachable regression, practical harm,
narrowest useful layer, existing overlapping cases, and cases or helpers deleted in the same
slice.

Pure tests construct values directly. They use no temporary repository, subprocess, mocked client,
or filesystem fixture. Focused examples cover the laws above. Generated cases use a small
reachable transition vocabulary such as submit, rewrite, reparent, insert, abandon, branch,
external rebase merge, squash merge, and fetch observation.

Generated cases have stable IDs, compact traces, canonical-state deduplication, bounded graph
size, and deterministic ordering. The oracle must not repeat the production traversal algorithm.
A small fixed corpus runs by default; expanded exploration remains opt-in.

Real-`jj` adapter tests are reserved for facts that depend on actual revset and DAG semantics:
collecting every divergent copy, trunk membership, untracked ancestors, overlapping descendants,
working-copy omission, boundary parents, conflicts, and merge parents. They do not duplicate the
pure policy matrix or assert thin query forwarding.

The fake GitHub ordinary merge endpoint must dispatch merge, rebase, and squash methods
accurately. One focused support test distinguishes their resulting commit topology. Remaining fake
idealizations are documented explicitly.

The faithful regression for the reported Voxel incident must:

1. submit a reviewed change and descendants;
2. insert an untracked change below it and rebase the reviewed local path;
3. externally rebase-merge the old submitted commit through an ordinary PR;
4. fetch the immutable trunk result beside the mutable local copy;
5. report one complete local path rather than a separate trunk-side stack;
6. resolve change-ID and PR selectors to the mutable actionable context;
7. stop before discarding the unpublished topology change, with the submitted, local, and trunk
   commits plus an actionable choice; and
8. leave the DAG, tracking, remote refs, PRs, reviews, and fake GitHub event log unchanged.

The PR in that regression has no native GitHub stack resource. Distinct boundary coverage also
includes clean singleton rebase recovery, a surviving reviewed suffix, squash recovery, multiple
mutable copies, trunk-only tracking, overlapping paths, a legacy local review bookmark, and
post-recovery orphan and cleanup agreement.

Reject these test smells:

- a fixture language that mirrors the production model;
- mocked-client tests that assert forwarding;
- a command-by-topology Cartesian matrix;
- impossible arbitrary DAGs presented as product scenarios;
- exact prose snapshots where typed outcomes and recovery data suffice;
- unit and integration copies of the same policy decision;
- unbounded or nondeterministic generation;
- retaining old tests temporarily beside tests of the replacement.

## Remaining slices

Every slice starts from the exact accepted commit of its dependencies. Its implementation brief
must repeat the objective, immediate adopters, required deletions, tests, documentation impact,
and review gate below. The implementing commit deletes its own slice section from this file.

### Slice 0: approve and record this plan

**Objective:** Review this execution plan, resolve omissions or ambiguity, and commit the accepted
plan before production work begins.

**Dependencies:** Current accepted repository tip.

**Changes:** This file only. No product behavior, canonical design, or implementation strategy
changes.

**Validation and review:** One independent reviewer checks that the plan is executable, pure-model
requirements are testable, replacement boundaries are explicit, and the plan does not compete
with `design.md`. User approval is the final gate.

### Slice 1: make ordinary merge tests faithful

**Objective:** Make the fake ordinary PR endpoint apply the requested merge method so an ordinary
rebase merge can be characterized honestly.

**Dependencies:** Accepted Slice 0 commit.

**Boundary and adopters:** Test support only; no production topology policy.

**Required deletion:** Remove comments, helpers, and cases that assume every ordinary merge is a
squash. Consolidate overlapping fake-merge coverage.

**Tests:** One boundary test proves merge, rebase, and squash create distinguishable topology. Run
the affected merge and sync integration tests plus `./check.py`.

**Documentation:** Update property-testing or fake-server documentation only if a stated
idealization changes.

**Review gate:** A test reviewer confirms the fake matches the observed GitHub contract and the
new case protects the boundary rather than request forwarding.

### Slice 2: replace selected-stack discovery with the pure model

**Objective:** Introduce the immutable observation snapshot and pure projection, then migrate
every selected-path consumer in one replacement.

**Dependencies:** Accepted Slice 1 commit and all decisions in the decision gate recorded in
`design.md`.

**Boundary and adopters:** External adapters batch `jj` and tracking facts. The pure model builds
the selected path and typed selection result. Immediate adopters are `view`, `submit`, `merge`,
selected `sync`, selected `unstack`, saved `checkout`, `relink`, and PR-based selection.

**Required deletion:** Delete `JjClient.discover_review_stack`, its boundary helpers and policy
flags, selected-path validation policy in the client, and selected command-local visible-copy or
path reconstruction. Remove superseded mocks, fixtures, and tests. Do not land an unused model or
a compatibility wrapper.

**Tests:** Pure examples and generated laws; focused real-`jj` observation tests; clean ordinary
singleton rebase recovery by change ID and PR selector; focused tests for each migrated command's
distinct mutation risk. Run `./check.py`.

**Documentation:** Update `design.md`, `implementation-strategy.md`, user workflow,
troubleshooting, and help wherever selected behavior changes. Introduce only terms that survive as
real types or enduring rules.

**Review gate:** Separate architecture and behavior reviewers confirm purity, selector semantics,
complete paths, deletion of the old selected authority, and consistency across all adopters.

### Slice 3: replace repository-wide discovery

**Objective:** Add repository-scoped observation without adding new topology rules, then migrate
every repository-wide consumer.

**Dependencies:** Accepted Slice 2 interface and exact commit.

**Boundary and adopters:** The same pure model receives a repository-complete snapshot. Immediate
adopters are `list`, connected advisories, stack pickers, orphan handling, cleanup classification,
`sync --all`, and `doctor` topology reporting.

**Required deletion:** Delete `review/discovery.py`, cleanup's parallel topology classifier,
change-ID-tuple path deduplication, old orphan membership logic, obsolete `LocalStack` policy, and
unused `jj` query helpers and tests.

**Tests:** Complete paths with untracked roots, immutable copies that do not become rows, shared
prefixes and overlaps, selected/repository agreement, trunk-only placement, and orphan/cleanup
agreement. Use integration coverage only for adapter and command-specific risks. Run `./check.py`.

**Documentation:** Update implementation strategy and any list, cleanup, orphan, checkout, or
doctor user guidance affected by the replacement.

**Review gate:** Reviewers confirm there is one topology authority, repository observation is
bounded and batched, no old wrapper remains, and every tracked change has one consistent
placement.

### Slice 4: reconcile external rebase merges without losing local work

**Objective:** Replace the merged-above-unmerged stop and generic divergence guidance with
planning over the accepted topology and existing trunk evidence.

**Dependencies:** Accepted Slices 2 and 3.

**Boundary and adopters:** Selected `sync` consumes topology facts and command-owned GitHub/trunk
evidence. The pure model does not decide that work merged or authorize mutation.

**Required deletion:** Remove the “sync separately” branch, duplicate divergent-change
diagnostics, and descendant/path checks now answered by topology. Consolidate overlapping
convergence tests.

**Tests:** The faithful Voxel regression, clean rebase-merged prefix with surviving reviews,
multiple mutable ambiguity, squash recovery, and zero mutation for unpublished topology. Run
`./check.py`.

**Documentation:** Update the sync policy in `design.md`, daily workflow, troubleshooting, and
help with concrete jj/GitHub language.

**Review gate:** Product and safety reviewers confirm normal recovery works, unpublished ordering
is preserved, diagnostics name runnable choices, and no patch-equivalence shortcut was added.

### Slice 5: implement explicit acceptance of GitHub's landed ordering

**Objective:** Implement the explicitly approved way to accept GitHub's ordering when local
topology changed after submission.

**Dependencies:** Accepted Slice 4 and the explicit UX, dry-run, and ordering decisions recorded
in `design.md`.

**Boundary and adopters:** Selected `sync` re-reads all mutation preconditions, removes only the
proven merged local copy, preserves every other local change, rebases survivors in the stated
order, updates existing reviews, and retires tracking under existing evidence rules.

**Required deletion:** Remove any manual-only stop superseded by the explicit operation. Add no
durable phase, alias, replay record, or second convergence path.

**Tests:** Dry-run, apply, interruption/retry through fresh observation, branch drift before
mutation, preservation of unreviewed and reviewed survivors, and the Voxel ordering outcome. Run
`./check.py`.

**Documentation:** Update canonical design, workflow, troubleshooting, help, and JSON behavior if
structured output changes. The final command or flag name must be ordinary and user-facing.

**Review gate:** Independent safety and UX reviewers confirm the authority is explicit, ordering
is understandable, preconditions are reread, work is preserved, and reruns need no durable state.

### Slice 6: diagnose tracked local review bookmarks

**Objective:** Detect local bookmarks named by saved review identities, including legacy head refs
outside the current reserved namespace.

**Dependencies:** Accepted repository topology model. This may run parallel to Slice 4 or 5 only
in a separate `jj workspace` and only if it touches no shared files or model contract.

**Boundary and adopters:** `doctor` and mutation preflight observe exact saved head refs. They do
not infer a legacy prefix or migrate bookmarks.

**Required deletion:** Remove any narrower duplicate check that the exact-identity check
supersedes. Add no compatibility state.

**Tests:** Conflicted and ordinary local tracked-head bookmarks, unrelated user bookmarks, exact
repair guidance, and no mutation without `doctor --fix` authority. Run `./check.py`.

**Documentation:** Update doctor and troubleshooting guidance in ordinary bookmark language.

**Review gate:** A reviewer confirms the check derives from current saved identity, cannot affect
unrelated bookmarks, and does not create a migration mechanism.

### Slice 7: cumulative command and documentation reconciliation

**Objective:** Exercise every lifecycle consumer against the accepted model and remove remaining
contradictions, adapters, tests, and terminology before the final frozen audit.

**Dependencies:** All behavior slices accepted.

**Boundary and adopters:** No new authority. This slice is deletion, consolidation, and correction
across commands and documentation.

**Required deletion:** Every superseded symbol named in the project-wide criteria, unused query or
model field, duplicated command policy, stale test helper, and obsolete documentation statement.

**Tests:** Focused command adoption tests, the default property corpus, expanded pure exploration,
and `./check.py`. Record before/after production and test lines, high-complexity functions, module
sizes, dependency fan-out, policy-authority count, and external call counts as review evidence.

**Documentation:** Read `design.md`, implementation strategy, all affected user docs, and help as
continuous prose. Ensure the canonical product story is coherent and implementation strategy
describes only the final architecture.

**Review gate:** Independent product/docs, architecture, and test reviewers agree that the tree is
ready to freeze for final audit.

## Final audit

Freeze one exact project tip. Run three independent reviews in parallel:

1. **Product and documentation:** Verify `design.md` is canonical and coherent; implementation
   strategy matches the code; user docs and help use ordinary jj/Git/GitHub vocabulary; no stale
   workflow, contradiction, or target-architecture prose remains.
2. **Architecture and complexity:** Prove purity and dependency direction; search for every
   superseded authority and wrapper; inspect before/after metrics; confirm no durable recovery
   state, parallel policy path, complexity spiral, or unexplained new terminology survives.
3. **Behavior and tests:** Walk the required scenario matrix; verify fake GitHub fidelity,
   selector and command consistency, property laws, mutation safety, runnable recovery guidance,
   and removal of overlapping tests.

Every finding creates a bounded remediation slice implemented and reviewed by separate agents.
Freeze a new tip and rerun every affected audit. Completion is declared only when all three audits
accept the same exact commit and `./check.py` has run with any budget decision reviewed
explicitly.

The last accepted project change deletes this file and nothing else. No one can guarantee success
in advance; the project is successful only when these gates provide the evidence and no required
work remains.
