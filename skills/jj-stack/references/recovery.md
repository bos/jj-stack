# Recovery workflows

Read this file for abnormal lifecycle state, repository-wide reconciliation, adoption, repair,
or cleanup beyond the common flow. Keep the universal safety and GitHub-write rules from
`SKILL.md` in force.

## Observe before choosing a repair

Run `doctor`, then inspect with explicit targets:

```text
list --json
view --pull-request <pr> --json
view <head-change-id>
```

`view` and `list` may exit 10 with valid output when inspection is incomplete or needs attention;
read the JSON before deciding. After interruption or in a multi-stack repository, never rely on
the default selection. Preview the chosen mutation with `--dry-run` when supported.

Do not resume a remembered plan. Every retry must use current `jj`, tracking, remote, and GitHub
observations. Use `jj op log` and `jj undo` for local recovery, never destructive Git commands.

## Reconcile completed merges

- After a queued or external merge finishes, run `sync --dry-run <head-change-id>`, then
  `sync <head-change-id>`. It fetches, proves what reached trunk, removes merged ancestors,
  rebases selected survivors, and updates only their existing PRs.
- Use the full head change ID when a rewritten survivor has several visible revisions. Let the
  selected `sync` prove which revision GitHub produced; do not choose a `/0` or `/1` revision or
  abandon a copy before that dry run.
- Do not run a separate `jj git fetch` merely to prepare this recovery. `sync` performs the
  required fetch itself; importing rewritten review branches first can create avoidable local
  divergence.
- If a direct merge completed but automatic sync failed, do not rerun `merge`; continue with the
  explicit selected `sync` printed by the command.
- If a queued PR is still waiting, do not submit or sync that stack. Independent stacks remain
  usable.
- If trunk merely advanced and none of the stack merged, use a bounded plain `jj rebase`; `sync`
  is not a general trunk-refresh command.

Use `sync --all --dry-run`, then `sync --all`, only for repository-wide reconciliation. It checks
every tracked PR and may retarget or close PRs and remove saved links when their exact submitted
commits are already on trunk. It never rebases local changes or submits a stack. When GitHub
produced rewritten merge commits, it leaves tracking in place and prints a selected
`sync <head-change-id>` for each affected stack.

## Recover an interrupted or rejected operation

After an interrupted `checkout` or `sync`, run `view` with the original explicit selector and
retry the same command from current observations.

For a rejected merge, fix the reported check, conflict, policy, or access problem. Rerun the same
explicit selector and merge method only when GitHub did not complete the merge. If a matching
request is pending, wait and observe it rather than starting another request.

## Adopt, repair, or forget tracking

- Use `checkout --pull-request <pr>` to fetch as needed and adopt the existing review through
  that PR, then edit the PR's change without rebasing changes or touching GitHub.
- Use `relink <pr> <revset>` when one known open PR and review branch must be attached to one
  existing local change.
- Use `unstack --local <head-change-id>` only to forget saved links without changing GitHub,
  review branches, PRs, or local history.

When recovering known lost tracking, inspect and adopt the known PR before any direct GitHub
mutation.

Do not overwrite or recreate a missing, moved, foreign, or ambiguous review branch. Follow the
reported `relink` or `unstack --local` path. If a direct structural GitHub mutation already
happened, inspect first and choose among `checkout`, `relink`, `submit`, `unstack`, or `cleanup`
from observed state; never rebuild changes or PRs by hand.

## Repair grouping and uncommon cleanup

When GitHub grouping no longer maps to one local path, preview the exact stack number from the
diagnostic with `unstack --dry-run --stack <number>`, then run `unstack --stack <number>`. This
removes grouping only and leaves PRs open. Never guess a stack number.

To start fresh reviews for the same changes, follow the closing and cleanup procedure in
`SKILL.md`, then run `submit <head-change-id>`. There is no restart flag; submitting before
cleanup does not replace the saved reviews.

For an orphan reported by `list`, first inspect its exact PR and verify its current live state. If
it is open and the user wants it removed, close it, then run
`cleanup --pull-request <pr> --dry-run` and `cleanup --pull-request <pr>`. After closing every
verified orphan, use `cleanup --pull-request orphans`. Orphan rows come from saved tracking and
do not by themselves prove live PR state.

## Diagnose local setup

Use `doctor --fix` only when a diagnostic names a repository setup defect that blocks the
requested task. It can restore the normal fetch exclusion for the reserved review-branch
namespace. A visible review bookmark is acceptable when it matches saved review state; repair
only a collision or mismatch named by the affected command. Do not run `doctor --fix` after a
successful operation as general cleanup. Use `doctor` for authentication, remote resolution, and
interrupted checkout or sync leftovers.
