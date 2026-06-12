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
- inspecting review state and landing ready changes

To create a review branch, `jj-stack` creates a bookmark with a well-defined prefix. By default
that prefix is `review/`, but you can choose a different prefix such as `my-review-stack/`.
These bookmarks are managed automatically, so you don't need to manage them yourself.
`jj-stack` creates them for review, forgets the local ones after `jj-stack land` lands
changes, and can also remove them later during `jj-stack unstack --cleanup` or `jj-stack
cleanup`.

## Source of truth

We use the local `jj` DAG as the source of truth for the stack: which changes exist, what order
they are in, and how they relate to each other.

To stay in sync with GitHub, `jj-stack` uses a small amount of supporting local metadata. That
metadata helps it:
- remember which GitHub PR goes with which local change
- keep the branch name of a review stable, even if you rewrite the change or its title
- safely resume or recover if a command is interrupted

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
request."  `jj-stack` creates `git` review branches only because GitHub requires them. Those
branches are a transport layer; the main authoring model is still local `jj` history.

## Practical rule

When in doubt:

- use `jj` to change the stack
- use `jj-stack view` to inspect the GitHub projection
- use `jj-stack submit` to refresh that projection
