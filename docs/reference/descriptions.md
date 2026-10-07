---
title: Pull request descriptions
linkTitle: PR descriptions
description: Set pull request titles, bodies, draft state, and stack overview text.
navGroup: Look things up
weight: 100
---

`jj-stack submit` takes each new PR's title and body from its `jj` change description. Later
submits refresh that text as you edit the change, provided the PR's title and body still match
the defaults for the last submitted version or another version in the change's `jj evolog`.

For example, suppose a submit creates the title `Add caching`. You rename it on GitHub to
`Cache API requests`, then change the local description. The next submit keeps both the GitHub
title and body. Editing either field on GitHub preserves the pair. Changing them on GitHub back
to the text of an earlier version of the change counts as no edit, so the next submit replaces
them.

Use `--describe` to replace a body explicitly, `--edit` to edit titles and bodies yourself, or
`--describe-with` to generate them with a helper.

## Default text

The first line of the change description becomes the PR title; the rest becomes its body. If
there is no body, jj-stack uses the repo's pull request template, or repeats the title if no
template exists.

jj-stack looks for `PULL_REQUEST_TEMPLATE.md` or `pull_request_template.md` in `.github/`, the
repo root, and `docs/`, in that order. It uses the first file it finds.

If no local file exists, jj-stack asks GitHub for the default template. This includes a template
on the repo's default branch or, when the repo has none, in its owner's public `.github`
repository, following GitHub's default community health file rules. An empty local template
overrides this fallback. jj-stack does not choose among named templates.

When the body comes from the change description, jj-stack removes line wrapping inside Markdown
paragraphs while preserving lists, quotes, tables, code blocks, and explicit line breaks.

## Supply Markdown

Set one pull request body explicitly. The title still follows the normal update rule:

```console
jj-stack submit --describe <change-id>=body.md
```

For a stack with more than one change, add an overview comment to the head pull request:

```console
jj-stack submit --describe stack=overview.md
```

Later submits preserve the overview, including edits made on GitHub, until you supply another
stack description. If the stack grows, jj-stack moves the overview to the new head PR. If you
abandon or squash away the head change and at least two changes remain, the next submit also
moves the overview to the new head PR.

Relative paths resolve from the directory in which you invoke jj-stack.

## Edit every PR at once

```console
jj-stack submit --edit
```

The editor opens once with every PR's title, body, and draft choice. Follow the instructions in
the file, then save and close it. If the document is invalid or the editor exits with an error,
submit stops before changing PR branches or pull requests.

jj-stack keeps the editor file until the whole submit succeeds and prints its path before
continuing. If submit fails, pass that file to `--resume-edit` when you retry:

```console
jj-stack submit --resume-edit /path/to/jj-stack-edit-….md
```

The retry inspects the local stack and GitHub again and reopens the file in your editor. The file
must still contain exactly the selected changes. jj-stack does not remove a file supplied with
`--resume-edit`; remove it yourself once submit succeeds.

The editor comes from jj's `ui.editor`, then `$VISUAL`, then `$EDITOR`.

## Delegate to a helper

```console
jj-stack submit --describe-with <helper>
```

jj-stack runs the executable from the repo root with `--pr <change-id>` once per change. For a
stack with more than one change, it also calls the helper with `--stack <revset>` to generate an
overview comment. Each invocation must print one JSON object with string `title` and `body`
fields:

```json
{"title": "add the API", "body": "Why this change exists.\n"}
```

For the `--stack` call, the `JJ_STACK_INPUT_FILE` environment variable points to a temporary JSON
file. Its `changes` array contains each change's `change_id`, generated `title` and `body`, and
`diffstat`, ordered from the bottom of the stack to its head. The helper can use this to summarize
the stack without generating the individual descriptions again.

The helper must exit successfully and write valid JSON. Send any diagnostics to standard error.
