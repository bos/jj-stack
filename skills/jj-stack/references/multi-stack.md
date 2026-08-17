# Multi-stack workflows

Read this file before changing pull requests whose local topology or GitHub grouping spans more
than one ordinary linear stack. Keep the universal identity, inspection, explicit-selection, and
GitHub-write rules from `SKILL.md` in force.

## Distinguish the two shapes

Derive local stack paths from the `jj` DAG. Treat GitHub stacks as remote groupings that
`jj-stack` reconciles from a selected local path. Do not infer saved boundaries from GitHub or
from a previous submit: only the current DAG and an explicit command boundary select work.

Use `list` for the repo-wide inventory and `view <head-change-id>` for each affected local
path. Select every mutation explicitly. Inspection may show one local path whose tracked changes
belong to several GitHub stacks; do not assume the displayed path is one GitHub stack.

At a fork, a bare change ID or linked PR for the shared ancestor is ambiguous because it asks for
the complete containing path and several descendant heads exist. The concrete choices are the
descendant heads.
To end a command exactly at the parent-stack head, pass an explicit revset such as
`change_id("<full-parent-head-change-id>")`; an arbitrary revset selects that exact commit
instead of searching for a containing head.

## Start or refresh a child stack

Use an exact submitted ancestor as a read-only boundary:

```text
submit --base <parent-change-id> <child-head-change-id>
```

This submits only `(parent, child-head]`; it does not update the parent PR. The parent must
be an exact open snapshot: its local commit, saved baseline, remote PR branch, and PR head
must match. Repeat `--base` on every refresh because jj-stack stores no boundary.

If the submitted base branch moved or disappeared, stop. jj-stack will not overwrite it. Report
the immutable submitted commit named by the diagnostic and require the user to restore that exact
remote branch externally before repeating the same bounded submit. Never restore it to the
parent's mutable change ID or guess from current local history.

When the user explicitly requests every sibling, use one bounded submit per sibling child. Keep
the shared fork in the parent stack. Do not merge a child stack while its bottom PR targets the
parent PR branch. After the parent PR lands:

1. Run `sync <parent-head-change-id>` if the merge was queued or external.
2. Rebase exactly the child range with
   `jj rebase -r '<child-bottom-change-id>::<child-head-change-id>' -o 'trunk()'`.
3. Run ordinary `submit <child-head-change-id>` without `--base`.

Use the bounded rebase so sibling paths remain untouched. Make this transition even when the PR
for a higher change in the parent stack remains open.

## Move changes between submitted stacks

Rewrite the local DAG with `jj`, then submit the source path before the destination path. The
source submit releases moved PRs from their old grouping; the destination submit joins them to
the new grouping. A destination-first attempt should fail before mutation.

Keep each moved PR attached to its change ID. Use ordinary `submit` when the destination is
trunk-based, or `submit --base B H` when its lower bound is submitted change `B`. Do not manually
unstack, close, recreate, or push PR branches unless a jj-stack diagnostic directs recovery.

## Split or join stacks

- To split at a fork, treat each maximal linear path separately. Keep the fork in the parent
  stack and submit every child path with its explicit submitted base. The first bounded submit
  may dissolve the old grouping; submit the other paths individually.
- To join linear stacks, rewrite them into one local chain and submit its resulting head. The
  submit reuses PRs by change ID, recalculates bases, dissolves completely selected old GitHub
  stacks, and creates the joined grouping.

Submit never updates pull requests outside its selected path. Old overview comments or stale PR
branches on paths not yet resubmitted are expected; use `list` to find each path that still needs
an explicit refresh.

## Handle grouping stops

`merge`, `sync`, and ordinary `unstack` require the active GitHub stack members they touch to
belong to one selected local parent chain. A non-maximal or partially overlapping selection may
stop rather than truncate another valid stack.

When a diagnostic says the remote grouping no longer maps to one local path, follow its exact
`unstack --stack <number>` instruction after reading [recovery workflows](recovery.md). Do not
guess a stack number or alter membership with `gh`.
