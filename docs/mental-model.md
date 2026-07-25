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

Each review branch is named `review/<subject-slug>-<short-change-id>`. The readable subject hints
at the change's purpose; the suffix ties the name to its stable change ID. The branches stay on
the Git remote, so they do not clutter local `jj` bookmark output. `jj-stack` creates them for
review and can remove them later during `jj-stack unstack --cleanup` or `jj-stack cleanup`.
Merging itself does not rewrite local history or remove review state; selected `sync` reconciles
GitHub's result first.

## Source of truth

We use the local `jj` DAG as the source of truth for the stack: which changes exist, what order
they are in, and how they relate to each other.

To stay in sync with GitHub, `jj-stack` uses a small amount of supporting local metadata. That
metadata helps it:

- remember which GitHub PR goes with which local change
- keep the branch name of a review stable, even if you rewrite the change or its title
- safely recover if a command is interrupted, by re-deriving what remains to do

This has a few consequences:

- Local rewrites are easy and flexible.
- `jj-stack` keeps only a small amount of supporting metadata. Your local `jj` history is still
  the source of truth for the stack.
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

When GitHub provides its stacked-PR feature, `jj-stack` registers the ordered PRs there. Otherwise
it shows the same navigation through managed PR comments. Native membership remains derived
GitHub state: the local DAG still decides which changes belong together.

Commands follow the selected change's parent chain. A reviewed change can be a common ancestor of
several local paths; operating on one path leaves its siblings alone. The only extra constraint
comes from an existing GitHub stack: if GitHub groups active PRs into one unit for an operation,
`jj-stack` stops rather than changing only an unsafe subset of that group.

## Practical rule

When in doubt:

- use `jj` to change the stack
- use `jj-stack view` to inspect the matching GitHub PR stack
- use `jj-stack submit` to refresh that PR stack
- use `jj-stack merge` to ask GitHub to merge the reviewed bottom changes
- use selected `jj-stack sync` afterward to reconcile local history
