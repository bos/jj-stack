---
title: Submit and update a stack
linkTitle: Submit and update
description: Create pull requests, control their descriptions and draft state, and refresh them.
navGroup: Everyday work
weight: 30
---

Run `jj-stack submit` to create pull requests for your stack or update them after local edits.
Each change gets one pull request, and the PR order follows your local history. Existing pull
requests keep their discussions as you revise the stack.

## Submit the current stack

From the head of your stack, run:

```console
jj-stack submit
```

To select another stack, pass its head change ID:

```console
jj-stack submit <head-change-id>
```

Use `jj-stack submit --dry-run` to preview the changes. Submitting pushes the PR branches and
updates GitHub; it does not rewrite your local history.

## Titles and descriptions

By default, a change's subject becomes its pull request title and the rest of its description
becomes the body. If there is no body, jj-stack uses the repo's pull request template, or repeats
the subject if no template exists. Later submits refresh this text unless you have customized it.

To edit the planned titles, bodies, and draft states in your editor before anything is pushed:

```console
jj-stack submit --edit
```

To supply a Markdown file as one pull request's body:

```console
jj-stack submit --describe <change-id>=pr-body.md
```

For a stack with several changes, `--describe stack=overview.md` adds an overview comment to the
head pull request. See [pull request descriptions](../reference/descriptions.md) for how text
updates work, how to reuse an editor file after a failed submit, and how to generate descriptions
with a helper program.

## Drafts and ready PRs

To create new pull requests as drafts:

```console
jj-stack submit --draft
```

Existing pull requests keep their draft status. Use `jj-stack submit --draft=all` to make every
PR in the stack a draft, or `jj-stack submit --open` to mark them ready for review. Use `--edit`
when only some PRs should be drafts.

## Reviewers and labels

Set reviewers and labels in your `jj` config to apply them when pull requests are created or
updated. For example, to request review from `octocat`, add this to your repo config:

```toml
[jj-stack]
reviewers = ["octocat"]
```

Command-line choices replace the corresponding configured defaults for that submit. For example,
`jj-stack submit --reviewers hubot --label needs-review` requests `hubot` and applies
`needs-review` to the stack's PRs. Existing labels and reviewer requests on GitHub are left in
place.

Explicit reviewer or label requests apply even to unchanged PRs. Configured defaults alone do
not cause unchanged PRs to be updated.

After addressing review feedback, you can ask the reviewers who approved or requested changes to
look again:

```console
jj-stack submit --re-request
```

## Revision history

After updates, jj-stack maintains a **Revision history** comment on each PR. It lists the PR's
recent versions with links to the diff between each version and the next, so reviewers can see
what changed since their last review.

For edits made directly on GitHub, see [work with a stack on GitHub](working-on-github.md).
If you move changes between stacks, follow
[multiple stacks](multiple-stacks.md#move-work-between-stacks).
