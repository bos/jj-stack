# Mental model

`jj-stack` is easiest to use when its job boundary is clear.

## What `jj` owns

You (or your agent) use `jj` to manage mutable local history in the ways you'd expect:

- splitting work into several changes
- reordering or rebasing those changes
- rewriting commit descriptions and diffs
- keeping the local DAG coherent

## What `jj-stack` owns

`jj-stack` takes care of turning your local changes into stacked GitHub PRs for a person or
agent to review:

- picking the selected linear stack
- assigning one `git` review branch and one PR per change in the stack
- setting the base branch for each PR
- refreshing those PRs after local rewrites
- inspecting review state and asking GitHub to merge reviewed changes

Each review branch is named `<prefix>/<subject-slug>-<short-change-id>`, where `<prefix>` is
`jj-stack` unless the repo sets `branch_prefix`. The readable subject hints at the change's
purpose; the suffix ties the name to its stable change ID. The branches normally stay on the Git
remote, so they do not clutter local `jj` bookmark output. `jj-stack` creates them for review and
can remove them later with `jj-stack cleanup` after their pull requests close or merge. When
GitHub completes a direct merge, `jj-stack merge` immediately fetches and brings local history in
line with it. After a queued merge or a merge completed through another client, run
`jj-stack sync <head-change-id>` once GitHub finishes.

## Stack structure

The local `jj` DAG determines which stack changes exist, their order, and their relationships.

To stay in sync with GitHub, `jj-stack` uses a small amount of supporting local metadata. That
metadata helps it:

- remember which GitHub PR goes with which local change
- keep the branch name of a review stable, even if you rewrite the change or its title
- safely recover if a command is interrupted, by re-deriving what remains to do

This has a few consequences:

- Local rewrites are easy and flexible.
- `jj-stack` keeps only a small amount of supporting metadata. Your local `jj` history still
  determines the stack.
- If `jj-stack` cannot tell which GitHub PR or branch belongs to a local change, it stops and
  asks you to fix the ambiguity instead of updating the wrong PR.

## What gets reviewed on GitHub

The "unit to review" is one visible mutable `jj` change. We issue one pull request per change,
from the bottom of the stack to its head. Often that bottom change sits directly
on `trunk()`, but it may also fork from a recent ancestor of `trunk()`. Each successive PR is
based on the preceding PR in the stack.

This allows you to escape from the trap of thinking about "one long-lived local branch per pull
request." `jj-stack` creates review branches only because GitHub requires them. Those branches are
a transport layer; the main authoring model is still local `jj` history.

A review first submitted with one change remains one ordinary PR. Once the review has at least
two PRs, `jj-stack` registers their order with GitHub's stack feature. After lower PRs
merge, GitHub may keep them in that group as history, so the remaining PR can still belong to the
existing group. The local DAG decides change order and ancestry. An ordinary trunk boundary or an
explicit `submit --base` boundary decides which selected changes form one GitHub review.

An explicit `submit --base <parent> <head>` makes the changes after that reviewed parent a
separate GitHub review. The boundary applies only to that command and is never saved, so repeat
`--base` when refreshing the child. After that exact base lands, use a bounded `jj rebase` to move
only the child range onto `trunk()`, then submit it normally, even if higher parent work remains.

Most commands follow the selected change's parent chain. `view` treats a bare change ID or linked
PR as the identity of a stack member and shows the complete containing stack instead. If the
local DAG has several possible containing heads, the identity is ambiguous and `view` stops.
An arbitrary revset still names an exact stack head.

`view` and `list` project those local DAG paths, so one report can contain changes belonging to
several native GitHub reviews. They display observed review membership but do not divide a path
into inferred review segments.

The existing GitHub stack remains a safety boundary: if GitHub groups active PRs into one unit
for an operation, `jj-stack` stops rather than changing only an unsafe subset of that group.

## Practical rule

When in doubt:

- use `jj` to change the stack
- use `jj-stack view` to inspect the matching GitHub PR stack
- use `jj-stack submit` to refresh that PR stack
- use `jj-stack merge` to ask GitHub to merge the reviewed bottom changes
- use `jj-stack sync <head-change-id>` to apply a queued or externally completed merge locally
  and refresh the PRs that remain, or to continue when automatic sync reports a local problem
