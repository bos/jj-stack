---
title: Pull request descriptions
linkTitle: PR descriptions
description: Set pull request titles, bodies, draft state, and stack overview text.
navGroup: Look things up
weight: 100
---

Every submit builds the pull request text again from the change description. If you override that
text, supply the override again on later submits that should keep it.

## Default text

For each change, the subject becomes the pull request title and the remainder becomes its body. If
the description has no body, jj-stack tries the repo's pull request template and then falls
back to the subject.

## Supply Markdown

Replace one pull request body while keeping its title from the change subject:

```console
jj-stack submit --describe <change-id>=body.md
```

Add a stack overview to the head pull request:

```console
jj-stack submit --describe stack=overview.md
```

Relative paths resolve from the directory in which you invoke jj-stack.

## Edit every PR at once

```console
jj-stack submit --edit
```

The editor opens once with every planned title, body, and draft choice. If the edited document is
invalid or the editor exits with an error, nothing is changed locally or on GitHub.

The editor comes from jj's `ui.editor`, then `$VISUAL`, then `$EDITOR`.

## Delegate to a helper

```console
jj-stack submit --describe-with <helper>
```

The executable receives `--pr <change-id>` once per change and `--stack <revset>` once for a
multi-change overview. It prints one JSON object:

```json
{"title": "add the API", "body": "Why this change exists.\n"}
```

Invalid or empty helper output stops the submit. A helper controls the text only; the order of
pull requests still comes from the order of the `jj` changes.
