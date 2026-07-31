# Recovery-aware topology project plan

> This is a temporary execution plan. It is authoritative for project scope, sequencing,
> ownership, and acceptance, but it is not a product specification.
> [design.md](docs/internals/design.md) remains the only authority for behavior that exists in
> the accepted product. Delete this file after the final project audit is accepted.

## Authority and lifecycle

This plan starts from accepted Slice 1 change `roykupmkqyyrnlpyvqsrotqrzyyllqkv`, exact commit
`db4176fbb7b692f2ce3e0af269e9ed22b00eea11`. New production work starts from Slice 1 plus the
accepted commit of this plan amendment.

The following rejected work is comparison evidence only:

- `rlyuqnzuorlmvktmrzxwxlnpnnkwkutp` at
  `2f4c5e5640ea6e08be820a8687fd85591255d99f`;
- `xptllrumkwovwvlrqrspnprykklnrmyo` at
  `424badbb1eb04678c785fb462a4f3c9da288a365`; and
- workspace change `wlwzutqrtuqpvxmvmzzqypwpzqytlkrr` at
  `12d950ae7f9ccaa379bd36ad38136a12bfa5a2df`.

None may be an ancestor or implementation base for a new slice. Their implementation content is
read-only comparison material. Do not move any of it onto the accepted line with `jj duplicate`,
a cherry-pick equivalent, rebase or transplant, patch application, diff replay, file or tree
checkout or restore, or wholesale or partial tree copying.

This restriction targets implementation content introduced or changed by the rejected artifacts;
it does not forbid independently using ordinary project concepts already present at Slice 1.
Every retained idea must be re-derived and implemented on the accepted Slice 1 plus plan tree,
then justified item by item in the deletion/non-port ledger. Preserve the rejected revisions and
workspaces until the accepted replacements and comparison reviews are complete.

This plan contains only work that remains or is in flight. A completed slice is deleted from this
file in the change that implements it. The implementing diff therefore shows both the promised
work and its removal. An independent reviewer decides whether that removal is justified. `jj`
history records completed work; this file does not become a changelog.

Behavioral decisions enter [design.md](docs/internals/design.md) with the code that implements
them, not as target-architecture prose beforehand.
[implementation-strategy.md](docs/internals/implementation-strategy.md) describes only
architecture that exists after the implementing change. User documentation and help change with
the behavior they explain.

The unimplemented accept-order operation in Slice 5 is the deliberate exception to keeping
pending product decisions out of this plan: its proposed UX, dry-run contract, and ordering
effect remain here until that behavior is implemented. Slice 5 moves the accepted rule into
`design.md` in the same commit as the code. Earlier slices must not describe it in `design.md` as
if it already exists.

## Product-judgment gate

Logical reachability is not a product requirement. Before adding or retaining handling for a
scenario, the implementer and product reviewer must answer:

1. Could an ordinary, mildly creative, or slightly mistaken human fairly easily reach it through
   normal-looking `jj`, GitHub, or `jj-stack` actions?
2. How likely is that path in actual use?
3. What harm follows if `jj-stack` has no special behavior?
4. How easily can a human identify and repair it with ordinary `jj` and GitHub tools?
5. How easily can an agent diagnose and repair it?
6. What permanent production, test, documentation, query, and conceptual complexity does special
   handling add?

First-class handling is justified only when probability, impact, and recovery difficulty
outweigh permanent complexity. A cheap polite stop is allowed only for a realistically
encountered state when the check and message are genuinely cheap. High impact alone does not
justify modeling a tortuous path, and easy reachability alone does not justify code when ordinary
or agent-assisted repair is quick.

A scenario that fails this gate is outside this project's product contract. It gets no dedicated
detection, typed outcome, diagnostic, fixture, generated transition, documentation, safety
guarantee, or architectural influence. Existing code or tests do not create a product
requirement.

The general off-happy-path prompts in the testing and code-review guides are discovery questions,
not prior authorization for a scenario. Every case proposed for this project still passes this
gate before it influences code or coverage.

The supported domain for this project is intentionally small: ordinary linear review stacks,
ordinary local inserts or reparents, ordinary GitHub rebase or squash merges, fetched trunk, and
normal overlapping branches created from shared local ancestors. The model may assume its
adapter has supplied that domain. It is not an exhaustive algebra for every visible `jj` graph.

## Scenario decisions

This table is execution evidence, not user documentation. A retained row authorizes only the
behavior in its decision cell; it does not authorize nearby combinations or a scenario matrix.

<table>
<thead>
<tr>
<th>Scenario</th>
<th>Human path</th>
<th>Likelihood</th>
<th>Harm</th>
<th>Manual/agent recovery</th>
<th>Implementation cost</th>
<th>Decision</th>
</tr>
</thead>
<tbody>
<tr>
<td>Reported Voxel incident</td>
<td>
Insert or reparent local work after submit; GitHub rebase-merges the reviewed commit; fetch.
</td>
<td>Real report; credible in normal stacked review.</td>
<td>A normal sync is blocked or unpublished ordering can be lost.</td>
<td>Manual repair is subtle; an agent can help, but the tool owns this workflow.</td>
<td>Moderate if modeled as one ordinary linear case.</td>
<td>Retain as the primary end-to-end acceptance case.</td>
</tr>
<tr>
<td>Clean single-review rebase merge</td>
<td>Submit one review, merge it with GitHub rebase merge, then sync.</td>
<td>Common where rebase merge is enabled.</td>
<td>The basic post-merge workflow fails.</td>
<td>Repair is possible, but users reasonably expect sync to do it.</td>
<td>Low incremental cost beside the reported incident.</td>
<td>Retain one representative.</td>
</tr>
<tr>
<td>Squash merge recovery</td>
<td>Submit normally, squash-merge on GitHub, then sync.</td>
<td>Common repository policy.</td>
<td>Existing core recovery regresses.</td>
<td>Manual repair is possible but unnecessarily error-prone.</td>
<td>Low: preserve the existing evidence path.</td>
<td>Retain existing behavior and one distinct regression.</td>
</tr>
<tr>
<td>Reviewed suffix survives a merged prefix</td>
<td>Submit a linear stack; merge its bottom review; keep reviewed changes above it.</td>
<td>Normal stacked-review use.</td>
<td>Surviving reviews can be misbased, replaced, or lose ordering.</td>
<td>Manual repair crosses local DAG and GitHub PR bases.</td>
<td>Moderate and central to selected sync.</td>
<td>Retain the smallest representative suffix.</td>
</tr>
<tr>
<td>Two mutable copies match a selector</td>
<td>Concurrent workspace operations leave two mutable copies of one reviewed change.</td>
<td>Uncommon, but easy enough with ordinary `jj` actions.</td>
<td>Selecting the wrong local copy could mutate the wrong review.</td>
<td>The user or an agent can choose or abandon a copy quickly.</td>
<td>Low if it is only a uniqueness check at selection.</td>
<td>Retain a cheap stop; add no rich topology or recovery UX.</td>
</tr>
<tr>
<td>Ordinary overlapping branches</td>
<td>Create two local branches from a shared review ancestor and select, view, or list either.</td>
<td>Ordinary `jj` use.</td>
<td>Paths can be conflated and the wrong PR base changed.</td>
<td>No repair should be needed for a normal shape.</td>
<td>Moderate; one shared-prefix rule serves selected and list views.</td>
<td>Retain selected, inventory, and connected-view representatives of the shared rule.</td>
</tr>
<tr>
<td>Network interruption and rerun</td>
<td>A normal mutation loses connectivity after an intended effect, then the user reruns it.</td>
<td>Routine distributed-system failure.</td>
<td>Duplicate or inconsistent remote effects are possible.</td>
<td>Rerun is easy when fresh observation is sufficient.</td>
<td>Low if no phase state or interruption matrix is added.</td>
<td>Retain fresh-observation retry and one distinct Slice 5 interruption case.</td>
</tr>
<tr>
<td>Review branch moves after planning</td>
<td>A user, teammate, or concurrent command moves a managed branch before mutation.</td>
<td>Uncommon but realistic.</td>
<td>The wrong ref or PR could be changed.</td>
<td>Fetch and inspect are straightforward for a human or agent.</td>
<td>Low because exact-target guards already exist.</td>
<td>Retain the existing precondition; add no topology classification.</td>
</tr>
</tbody>
</table>

Two considered candidates do not add topology scope:

- Tracking whose local change is gone remains ordinary identity and cleanup work under the
  existing design. Do not invent a trunk-only path, placement outcome, or recovery scenario for
  it. Slice 3 may reuse a simple membership fact only if a current consumer already needs it.
- Saved review-head bookmarks from obsolete development behavior do not pass the gate. Delete the
  former dedicated slice. Do not add prefix inference, exact-identity detection, repair guidance,
  fixtures, documentation, or migration behavior for them.

## Coordination and review protocol

The primary agent is the coordinator. It owns this plan, dependency ordering, exact revision
handoffs, `jj` integration, and go/no-go decisions. It does not implement or review its own code
slices.

Each implementation slice follows this protocol:

1. The coordinator records the accepted parent change ID and exact commit ID.
2. One implementation agent receives a bounded brief naming this plan section, the retained
   scenario rows it serves, and the canonical documents it must read.
3. Isolated work uses a sibling `jj workspace`, never a Git worktree. Only disjoint adopters run
   in parallel after the pure interface is accepted.
4. The implementer reports the change ID, exact commit ID, tests, deletions, documentation
   changes, before/after evidence, deletion/non-port ledger, and unresolved risks.
5. A different agent reviews that exact commit against
   [design.md](docs/internals/design.md),
   [testing-philosophy.md](docs/internals/testing-philosophy.md), and
   [code-reviews.md](docs/internals/code-reviews.md).
6. Findings return to the implementer. Any changed commit ID receives another exact-commit
   review.
7. The coordinator runs or verifies focused tests and `./check.py`, checks promised deletions and
   scope, and accepts or rejects the slice.

Agents receive this plan, the files named by their slice, and concise predecessor reports. They
do not inherit the full project conversation. A reviewer reads the frozen artifact and canonical
documents rather than relying on an implementer's summary.

The plan section for a slice is deleted only in the exact implementation commit accepted by the
reviewer. Project-wide criteria and final-audit sections remain until the project ends.

## Deletion and non-port protocol

Slices 2 and 3 must inspect the rejected comparison diffs against Slice 1 before implementation.
Their reports include a ledger covering every introduced or expanded:

- model type, field, and problem reason;
- traversal branch, query, and adapter;
- diagnostic, status conversion, and command-local recovery branch;
- test fixture, helper, oracle, generated transition, and fixed scenario; and
- help, user-documentation, design, and implementation-strategy statement.

For each item, the ledger records either:

- **independently re-derive**, with the retained scenario row and present consumer that justify
  rebuilding the idea from the accepted tree; or
- **do not port**, with the deleted or avoided mechanism named.

There is no presumption in favor of code that already exists in a rejected revision. A generic
problem framework, complete-graph snapshot, catch-all result, or diagnostic adapter must not
survive merely because several rejected callers already use it. If its sole justification was
universal reachability or an excluded scenario, it is deleted or not ported.

The ledger does not authorize copying an accepted idea's rejected implementation. Read-only
inspection establishes comparison evidence; implementation begins from the accepted tree.

The accepted implementation diff itself must show the replacement and deletion. The ledger is
review evidence, not a permanent compatibility record or new source of product policy.

## Project-wide success criteria

### Small authority for ordinary local review paths

- One pure model derives selected linear paths and repository path inventory only for the
  supported domain.
- Selection by revision, change ID, or PR uses the same ordinary-path lookup.
- A mutable local copy beside the immutable fetched-trunk result of an ordinary rebase merge
  resolves to the actionable local path.
- More than one mutable match stops selection through one cheap uniqueness rule.
- Shared ancestors in ordinary overlapping branches have one observed identity and may appear in
  each rendered path.
- The connected `view` stale-stack advisory uses that same ordinary shared-prefix and
  path-membership projection, with no command-local topology observer.
- Tracking annotates a derived path; it does not create topology.
- Commands use the model only for facts it owns. Identity, GitHub merge evidence, cleanup
  eligibility, and mutation preconditions remain command-layer policies rather than being forced
  into a topology result.
- No durable topology, replay, transaction, alias, or migration state is added.

The model need not account for every visible revision, describe every rejected shape, reconstruct
a pseudo-stack after its supported preconditions fail, or promise a result for every observation.
Adapters may reject an unsupported selection through existing command boundaries without adding
a taxonomy or scenario-specific message.

### Pure boundary

The model accepts immutable typed observations and returns immutable typed ordinary-path
projections. It cannot reach a subprocess, `jj` or Git client, filesystem path, state store,
GitHub client, configuration, clock, callback, lazy query, global state, or mutation API.

Adapters collect and batch the bounded external facts needed for the retained scenarios before
calling the model. Dependent mutations remain ordered and re-read their preconditions immediately
before changing state.

The required model laws are limited to the supported domain:

- input ordering does not change an ordinary path result;
- every returned path is parent-connected and preserves local ancestor order;
- tracking does not create or reorder a path;
- the unique mutable-copy selection is stable when its immutable trunk copy is observed; and
- selected and repository projections agree for the ordinary path they share.

Do not add a law that quantifies over all reachable observations or arbitrary graphs.

### Recovery behavior

- The faithful Voxel regression resolves change-ID and PR selectors to the local actionable
  context after fetch.
- The default recovery action does not discard unpublished local work or ordering.
- Clean ordinary rebase and existing squash recovery continue to work.
- A merged prefix with a reviewed suffix preserves the suffix's review identity and ordering.
- Accepting GitHub's landed ordering requires the explicit authority implemented in Slice 5.
- A rerun observes current state; no durable recovery phase or replay plan is saved.

These guarantees apply only to the retained scenarios and existing supported product behavior.
They are not a general promise for every constructible repository state.

### Replacement, not accumulation

The finished project contains one authority for each local path fact it changes. In the accepted
supported domain, remove:

- `JjClient.discover_review_stack`, its stack-boundary policy flags, and selected command-local
  path reconstruction;
- repository discovery functions superseded by the ordinary-path projection;
- change-ID tuple path deduplication superseded by observed revision identity;
- separate selector rules for visible-copy and path membership;
- command-local descendant or placement decisions answered by the shared projection;
- compatibility wrappers preserving old and new paths; and
- tests and helpers whose only purpose was a deleted authority or rejected scenario.

Do not rewrite cleanup, orphan, doctor, or lifecycle code merely to make every command consume one
large result. Share a fact only where two current consumers actually decide the same thing.

### Complexity evidence and stop

Every code slice reports before and after:

- production, test, and total lines;
- model types and fields, classes, functions, and Ruff `C901` findings;
- affected module sizes and dependency fan-out;
- count and location of path, selection, copy, placement, and dependency authorities; and
- number of `jj`, remote, and GitHub observations on the retained workflows.

Compare the accepted Slice 1 baseline, the rejected comparison work, and the proposed slice.
Metrics are evidence, not line-count targets. Do not compress code, merge responsibilities, or
move policy between files to game them.

If the replacement materially exceeds the production or conceptual complexity of the mechanisms
it deletes, stop the design. Do not accept a budget increase, promise a later cleanup, or proceed
to the next slice. Re-derive the smallest ordinary-workflow model and obtain independent product
and complexity approval.

## Test strategy and acceptance

Before adding or retaining a test, inventory existing coverage and record:

- the retained scenario and user-reachable regression;
- practical harm;
- the narrowest meaningful layer;
- overlapping cases to consolidate or delete; and
- fixture, helper, or rejected-rewrite coverage removed in the same slice.

Pure tests construct values directly. Their examples cover only the supported-domain laws and
retained decisions above. There is no universal graph oracle. Generated cases are optional and
must add independent value beyond focused examples. If used, generation is limited to supported
ordinary transitions, bounded and deterministic, and must not turn arbitrary reachability into
scope.

Real-`jj` adapter tests are reserved for ordinary facts that depend on actual revset and DAG
semantics: the selected linear chain, one fetched immutable trunk copy, one shared-prefix branch,
and omission of repository working copies where current inventory requires it. They do not
duplicate pure policy decisions or assert query forwarding.

The fake GitHub ordinary merge endpoint continues to distinguish merge, rebase, and squash
topology accurately. One focused support test protects that boundary.

The faithful regression for the reported Voxel incident must:

1. submit a normally reviewed linear stack;
2. insert an untracked local change below the reviewed change and reparent the local path;
3. externally rebase-merge the old submitted commit through an ordinary PR;
4. fetch the immutable trunk result beside the mutable local copy;
5. report one ordinary actionable local path, not a separate trunk-side stack;
6. resolve change-ID and PR selectors to that local path;
7. stop before discarding the unpublished ordering, naming the submitted, local, and trunk
   commits plus the available ordinary next step; and
8. leave the DAG, tracking, remote refs, PRs, reviews, and fake GitHub event log unchanged.

The stop in step 7 is required because this is the reported ordinary incident, not because all
unsupported states receive complete diagnostics.

Keep one focused example for each other retained risk only when another existing test would not
catch the same regression. Reject:

- command-by-topology or topology-by-diagnostic matrices;
- arbitrary DAGs presented as product scenarios;
- typed problems or exact messages for excluded states;
- generated composite histories outside the supported ordinary transition vocabulary;
- unit and integration copies of the same policy decision;
- a fixture language mirroring the production model; and
- old tests retained temporarily beside replacement tests.

## Remaining slices

Every slice starts from the exact accepted commit of its dependencies. Its brief repeats the
objective, immediate adopters, required deletions, retained scenarios, tests, documentation
impact, complexity evidence, and review gate. The implementing commit deletes its own section
from this file.

### Slice 3: replace ordinary repository path discovery

**Objective:** Extend the accepted small projection to inventory ordinary linear paths with
shared prefixes, then replace only repository-wide code that decides the same path facts.

**Dependencies:** Accepted Slice 2 interface and exact commit.

**Boundary and adopters:** The adapter batches visible off-trunk candidates required by current
inventory plus copies of tracked or selected change IDs needed by retained workflows. The model
returns ordinary maximal paths and shared revision identity. Immediate adopters are `list`, the
ordinary dependency check used by selected recovery, the connected `view` stale-stack advisory,
and any existing repository stack picker that currently reconstructs those paths.

The `view` advisory uses the same shared-prefix and path-membership projection to find connected
tracked stacks. Its existing baseline comparison and rendering remain outside the model. Delete
its command-local topology observer and policy; do not broaden `view` into repository-wide
inspection or route unrelated `view` behavior through the repository projection.

Cleanup, orphan reporting, `sync --all`, and `doctor` consume the projection only for a path or
membership fact they demonstrably need. Their GitHub evidence, identity, cleanup eligibility, and
setup diagnostics stay outside the model. Do not add a topology outcome merely to route every
command through one object.

**Retained scenarios:** One ordinary shared-prefix inventory, selected/repository agreement for
that path, the existing connected `view` stale-stack advisory for the same path, and the
dependency needed to preserve the reviewed suffix in the reported workflow.

**Required deletion:** Delete `review/discovery.py` and repository discovery, tuple
deduplication, placement, picker, or descendant logic actually superseded by the accepted
projection. Delete stale classifiers only where they decide that same path fact. Remove obsolete
`LocalStack` policy, query helpers, tests, and rejected-rewrite adapters that no accepted consumer
needs. Complete the second deletion/non-port ledger before review.

**Tests:** One pure shared-prefix example, one selected/repository agreement example, one
real-`jj` ordinary overlap boundary, the existing focused connected-`view` advisory behavior, and
focused command tests for distinct current risks. The advisory test protects only connection
scope and identification of the other stale stack; baseline comparison and rendering reuse their
existing coverage. Tracking without a local change remains existing cleanup evidence, not a
synthetic path fixture. Run `./check.py`.

**Documentation:** Update implementation strategy, the current cross-stack rule in `design.md`,
and `daily-workflow.md` guidance for the connected `view` advisory in the same slice. Preserve
the existing current-product behavior without adding unsupported graph taxonomy.

**Review gate:** Reviewers confirm one authority for ordinary path facts, including the connected
`view` advisory; bounded batched observation; no command-local connected-stack observer; no
universal snapshot or catch-all problem framework; deletion of superseded code; and acceptable
before/after complexity.

### Slice 4: reconcile ordinary external merges without losing local work

**Objective:** Replace the merged-above-unmerged stop and generic divergence guidance with a
small recovery plan for the retained external-merge workflows.

**Dependencies:** Accepted Slices 2 and 3.

**Boundary and adopters:** Selected `sync` consumes the ordinary path plus command-owned
GitHub/trunk evidence. The pure path model neither declares work merged nor authorizes mutation.

**Retained scenarios:** The faithful Voxel incident, clean rebase recovery, existing squash
recovery, and a merged prefix with the smallest reviewed suffix.

**Required deletion:** Remove the superseded “sync separately” branch, duplicated diagnostics,
and descendant/path checks now answered by the ordinary projection. Delete rejected-rewrite
problem conversions and scenario coverage that do not serve a retained row.

**Tests:** The faithful Voxel regression, one clean rebase case, existing squash recovery, and one
reviewed-suffix case. Consolidate overlaps. The Voxel default stop proves no mutation. Run
`./check.py`.

**Documentation:** Update the current sync policy in `design.md`, daily workflow,
troubleshooting, and help using concrete `jj` and GitHub language.

**Review gate:** Independent product and safety reviewers confirm the reported and common
recovery paths work, unpublished ordering is preserved, the normal stop is actionable, and no
general topology-defense framework was added.

### Slice 5: explicitly accept GitHub's landed ordering

**Objective:** Implement an explicit operation for the user to accept GitHub's ordering after the
ordinary local insert/reparent represented by the Voxel incident.

**Dependencies:** Accepted Slice 4 and independent approval of the exact UX below. Keep the
pending UX in this plan until the implementing commit; do not pre-write it into `design.md`.

**Pending product decision:** Choose one ordinary command or flag, with normal `--dry-run`
preview, that says GitHub already landed the reviewed change and the user now authorizes the
formerly lower unpublished change to follow it. The preview must show the resulting order in
change IDs and distinguish the immutable trunk commit when its commit ID matters. It must not ask
the user to understand topology-model terminology. Final spelling and confirmation semantics
require product review before implementation.

**Boundary and adopters:** Selected `sync` re-observes the selected path, trunk evidence, live PR,
and review branch immediately before mutation. It removes only the proven merged local copy,
preserves every other local change, rebases survivors in the previewed order, updates existing
reviews, and retires tracking under existing evidence rules.

**Retained scenarios:** The Voxel ordering outcome, preservation of the smallest reviewed suffix,
one network interruption followed by fresh-observation rerun, and existing exact-target branch
drift immediately before mutation.

**Required deletion:** Remove the manual-only stop superseded by the explicit operation. Add no
durable phase, alias, replay record, alternate convergence path, transition taxonomy, or
interruption matrix.

**Tests:** Preview, apply, the faithful ordering result, preservation of unreviewed and reviewed
survivors, one distinct interruption/rerun, and existing branch guard coverage. Do not combine
those boundaries into exotic histories. Run `./check.py`.

**Documentation:** In this implementing commit, move the accepted rule from this plan into
canonical design, workflow, troubleshooting, help, and JSON documentation if structured output
changes.

**Review gate:** Independent safety and UX reviewers confirm explicit authority, understandable
ordering, immediate precondition rereads, work preservation, ordinary rerun behavior, and no
durable recovery state.

### Slice 6: cumulative deletion and reconciliation

**Objective:** Remove remaining superseded or rejected machinery and reconcile current code,
tests, help, and documentation before the frozen final audit.

**Dependencies:** All behavior slices accepted.

**Boundary and adopters:** No new product behavior or authority. This slice is deletion,
consolidation, and correction only.

**Required deletion:** Every superseded symbol named in project-wide criteria; every rejected
model field, problem, adapter, fixture, scenario, or diagnostic without a retained-scenario
justification; every unused query; and every target-architecture or obsolete product statement.

Complete a cumulative non-port ledger against all three comparison artifacts. Record before/after
production and test lines, functions and types, `C901` findings, module sizes, dependency fan-out,
policy-authority count, and external call counts. A material complexity increase is a design stop,
not an invitation to edit a budget.

**Tests:** The focused retained corpus, any supported-domain generated examples that independently
earned their place, and `./check.py`. Remove overlapping and excluded-scenario tests.

**Documentation:** Read canonical design, implementation strategy, affected user docs, and help
as continuous prose. They describe the implemented product only.

**Review gate:** Independent product/docs, architecture/complexity, and test reviewers accept the
same exact commit and agree the tree is ready to freeze.

## Final audit

Freeze one exact project tip. Run three independent reviews in parallel:

1. **Product and documentation:** Apply the product-judgment gate to every retained special case.
   Verify `design.md` is canonical and current, implementation strategy matches the code, user
   docs and help use ordinary vocabulary, and no excluded or unimplemented behavior remains.
2. **Architecture and complexity:** Prove the small model's purity and dependency direction.
   Search for every superseded authority and every rejected artifact in the cumulative ledger.
   Verify accepted implementation content was independently derived on the Slice 1 plus plan
   line, then inspect before/after metrics and external call counts. Confirm no durable recovery
   state, universal graph contract, generic catch-all framework, parallel policy path, or
   unexplained terminology survives.
3. **Behavior and tests:** Replay the retained scenario table at the narrowest useful layers.
   Verify the Voxel regression, ordinary rebase and squash recovery, reviewed-suffix
   preservation, explicit authority, and deletion of overlapping or excluded coverage.

Every finding creates a bounded remediation slice implemented and reviewed by different agents.
Freeze a new tip and rerun every affected audit. Completion requires all three audits to accept
the same exact commit and `./check.py` to pass without an unreviewed budget increase.

After accepted replacements have passed comparison and final review, the coordinator uses only
`jj` to abandon or otherwise supersede the obsolete rewrite changes and forget their temporary
workspaces, then removes the workspace directories. Preserve them until that point so review can
compare exact artifacts; they must not remain as plausible implementation heads afterward.

The last accepted project change deletes this file and nothing else. Completion is declared only
when all gates accept the frozen commit and no required work remains.
