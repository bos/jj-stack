# Generated integration testing

These constraints supplement the [testing philosophy](testing-philosophy.md):

- In `StackMachine`, keep server events and recovery as separate actions. A server merge must
  leave room for local edits before sync; an interrupted submit must leave room for edits or
  remote changes before retry. Combining these actions would hide the inconsistent states the
  harness exists to test.
- Gate an action only on what its command or GitHub itself requires. `server_merge` merges any
  PR GitHub's UI would, whether or not the local stack still matches the GitHub stack and before
  any sync, because GitHub merges by PR and rewrites along its own stack. `merge` requires only
  that the merged changes match their PRs, and `submit` may stop at a change in the middle of a
  path. A precondition that waits for local history and GitHub to agree hides the states where
  they do not.
- An external stack merge may leave the survivors' rewrite pending, and `server_rewrite`
  completes it later, rooted on trunk's tip at that time as GitHub does, optionally after
  advancing trunk itself. Keep the merge and the rewrite separate actions so local work and
  other server events can happen between them.
- `interrupt_sync` fails a sync after it has rewritten the local stack and updated the
  surviving PRs but before it cleans up the merged ones, the state a crashed or interrupted sync
  leaves behind. Recovery must come from a later `sync`, `sync --all`, or `cleanup`, so keep the
  interruption separate from those actions.
- Shared-file edits use single-line replacements, and generated moves preserve the relative order
  of changes that edit the same file. These restrictions let the harness predict conflicts without
  implementing `jj`'s merge algorithm; broadening the edits requires revisiting that assumption.
- Each independent seeded search (shard) in the property tests has its own Hypothesis example
  database. Sharing a database would spend parallel search time replaying and shrinking the same
  saved failure.
- The generated search runs through `just property` and CI's smoke job; `just check` runs only
  the fixed scenarios in `tests/property`. The runner ends by listing the rules the sequences
  fired and the rules none reached, including the `state:` entries in `STATES` for cross-system
  states the harness exists to reach. Treat an unreached rule or state as a search problem, not
  as missing coverage: give it a richer starting state or fix its precondition.
- A rule is eligible only when it has work to do; compute its candidates in a helper that its
  precondition shares. Hypothesis picks uniformly among eligible rules, so a rule that finds
  nothing wastes a step and a model check.
- Server drift and injected faults are capped per sequence, and some sequences start after an
  external merge of the bottom PR. Each disturbance pushes the state into a stop that later
  commands only confirm, and every recovery rule needs a merged PR first; without both, a short
  walk never reaches recovery.
- `check_contents` skips a change whose commit and expected contents it already verified. A new
  content check that depends on anything else must extend that key.
