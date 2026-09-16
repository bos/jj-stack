---
title: How jj-stack compares with other tools
linkTitle: Compare tools
description: Choose a GitHub PR workflow that fits how you use jj or Git.
navGroup: Look things up
weight: 115
---

## jj-stack is opinionated

On GitHub, a pull request is the head of a graph of commits. A GitHub stack builds on this: a
stack is a linear chain of PRs. This makes a stack an odd structure: a chain whose links are
themselves graphs of commits.

`jj-stack` takes a simpler stance: it requires each PR in a stack to be a single `jj` change.
Supporting multi-commit PRs in a stack would add complexity that exists mainly for backwards
compatibility with branch-based workflows, and I don't think it has merit of its own.

This is also why `jj-stack` manages the refs that keep the PRs in a stack alive. They're not
valuable, they're merely `git` plumbing getting in your way.

While these opinions make for a much nicer default experience, they close some doors:
if you genuinely want to produce weird stacks-of-DAGs that `gh stack` would handle, `jj-stack`
may prevent that. If you think naming your PR branches is a good use of your time, `jj-stack`
will get in your way! I can imagine a world in which these opinions are too narrow and should be
revised, so if there's enough pressure to rethink them, I may do so.

## How the tools differ

Each of the tools below can turn local work into GitHub pull requests. They disagree about what
you should manage yourself and how much of the workflow the tool should own.

`jj-stack` manages a stack as a unit, and works with GitHub's native stack concept. The parent
order visible in `jj log` determines the PR order, and the same tool handles submission,
merging, local updates after a merge, and cleanup.

The main reasons to consider an alternative are:

- [`jj-spr`][jj-spr] keeps each submitted version in the PR's commit history, so reviewers can
  follow updates through GitHub's commit list.
- [`jj-gh`][jj-gh] adds GitHub commands to your own bookmark workflow, including PR and CI
  information in `jj log`.
- [`gh stack`][gh-stack] manages native GitHub stacks built from named Git branches, with room
  for several commits in each PR.

Do not use more than one of these tools to update the same pull requests or PR branches. Each
tool makes different assumptions about who owns those branches.

## `jj-stack` and `jj-spr`

`jj-spr` is the closest comparison. Both tools let you amend and rebase a `jj` change
without opening a replacement pull request. Both create generated branches on GitHub and can
publish one pull request per change. Only `jj-stack` manages native GitHub stacks.

### How reviewers see updates

`jj-spr` adds a commit to the PR branch for each submitted update, along with an update message
from the author. Reviewers can use GitHub's commit list to follow each version and see what
changed. Those update commits are squash-merged into one commit when the PR lands.

`jj-stack` force-pushes the current version of your change and maintains a **Revision history**
comment with links to earlier versions and the differences between them. You don't need to write
an update-commit message, but reviewers must use that comment to compare versions, as the
"Changes since your last review" view is empty when you use GitHub stacks. (`gh stack` has the
same problem. Looks like a bug in GitHub!)

### Dependent and independent changes

`jj-spr` can publish dependent PRs, but is currently incompatible with GitHub's native stacks.
(It gives each dependent PR a separate base branch containing the preceding change's files;
[native stacks require][github-stacks] that PR to target the preceding PR's branch.)

Its cherry-pick mode also lets you publish changes from a local chain as independent PRs that
can land in any order.

`jj-stack` turns a chain of two or more changes into a native GitHub stack and lands it from the
bottom upward. You rearrange the changes with `jj`, then run `jj-stack submit` to update GitHub.
Independent work belongs in [separate local stacks](guides/multiple-stacks.md), which you can
combine locally with a megamerge.

### Landing and cleanup

`jj-spr land` squash-merges one pull request. You then fetch, rebase your remaining local
changes, and resubmit any dependent PRs yourself.

`jj-stack merge` merges ready PRs from the bottom of the stack. For direct merges, which GitHub
performs immediately, it uses your explicit method or prefers rebase, then squash, then a merge
commit among the allowed methods. When several methods are allowed, signed stacks require an
explicit choice because merging can discard signatures.

It waits through merge queues, updates your remaining local changes and PRs, and cleans up unused
branches. If you stop waiting or merge through GitHub, `jj-stack sync` handles that follow-up
work. See [Merge and sync](guides/merge-and-sync.md) for the workflow.

## `jj-stack` and `jj-gh`

`jj-gh` is a set of focused GitHub helpers installed as `jj` aliases. It creates and edits PRs,
adds PR and CI information to `jj log`, and offers conveniences such as retrying CI and enabling
auto-merge.

You work with bookmarks, and each PR can contain several commits. `jj-gh` can update PR bases
after you rearrange local history, but you continue to decide when and how to push bookmarks.

It suits a workflow where you want individual GitHub commands while keeping control of your
bookmarks. `jj-stack` takes on more of that work: it manages PR branches, submits the stack as a
unit, and handles merging and cleanup. It does not replace `jj-gh`'s log and CI conveniences.

## `jj-stack` and `gh stack`

Both `gh stack` and `jj-stack` create native GitHub stacks. Their local models are different.

`gh stack` organizes work as named Git branches. A branch can contain several commits, and
`gh stack` creates one PR per branch. Its commands and interactive editor help you add,
reorder, rebase, and combine those branches. See the [`gh stack` guide][gh-stack].

With `jj-stack`, you make those edits using `jj` commands such as `jj split`, `jj squash`, and
`jj arrange`. There is no separate list of stack branches to maintain: submitting reads the
current local history and updates GitHub to match.

## Research notes

The linked project documentation was checked on September 6, 2026. These tools are changing;
check their current guides for requirements and detailed command behavior.

[jj-spr]: https://github.com/jennings/jj-spr
[github-stacks]: https://docs.github.com/en/rest/pulls/stacks#create-a-pull-request-stack
[jj-gh]: https://github.com/mrjones2014/jj-gh
[gh-stack]: https://github.com/github/gh-stack
