# Writing PR Descriptions

`jj-stack submit` derives every pull request title and body from your `jj` changes on each run.
This page covers the three ways to override that: supplying Markdown files, opening an editor, and
delegating to a helper program.

Because descriptions are re-derived on every submit, an override you want to keep has to be
supplied on every submit that should keep it.

## The default

For each change in the stack:

- the PR title is the commit subject
- the PR body is the rest of the commit description

If a change has no description body, `jj-stack` falls back to your repository's pull request
template, and finally to the subject, so a PR never opens with a blank comment.

The template is the first of these that exists under the workspace root, in either upper- or
lower-case:

- `.github/PULL_REQUEST_TEMPLATE.md`
- `PULL_REQUEST_TEMPLATE.md`
- `docs/PULL_REQUEST_TEMPLATE.md`

An empty template counts as missing. The template never overrides a change description body or
anything you pass with `--describe` or `--describe-with`.

## Supplying Markdown files

```bash
jj-stack submit --describe <change>=<file>
```

Replaces one PR's body with the Markdown in `<file>`, keeping the title from the change subject.
The `<change>` selector must resolve to exactly one change in the selected stack.

```bash
jj-stack submit --describe stack=<file>
```

Uses the Markdown in `<file>` as the stack overview comment on the head PR. This applies to a
stack of more than one change.

`--describe` can be repeated. Relative paths resolve from the directory you ran `jj-stack` in, not
from the repository root.

## Editing before submitting

```bash
jj-stack submit --edit
```

Opens your editor once, containing the planned title and body of every PR in the stack, ordered
top-to-bottom the way `view` shows them, and pre-filled from the defaults above including any
`--describe` files. What you save replaces those titles and bodies.

The editor is whatever jj's `ui.editor` resolves to, including its `$VISUAL` and `$EDITOR`
fallbacks.

`submit` aborts before changing anything — locally, on the remote, or on GitHub — if the
editor exits non-zero, or if the saved document has content before the first change separator, an
unknown, repeated, or missing change section, or a section with no title line.

`--edit` cannot be combined with `--describe-with`, since a helper already owns description
authoring. It does compose with `--describe`.

## Delegating to a helper

```bash
jj-stack submit --describe-with <helper>
```

Replaces the defaults by invoking your program two ways:

- `helper --pr <change_id>` runs once per change, and supplies that PR's title and body.
- `helper --stack <revset>` runs once for the stack, only when it holds more than one change, and
  supplies the stack overview comment on the head PR. To write no overview comment, return empty
  `title` and `body` fields; any existing overview comment is then removed. Printing nothing at
  all aborts the submit.

### Helper input

For the per-stack invocation, `jj-stack` writes a temporary file containing each PR's title, body,
and a compact diffstat, and sets `JJ_STACK_INPUT_FILE` to its path. Reading that file lets a
helper summarize from PR-level metadata instead of replaying the whole patch series.

### Helper output

Output must be structured. Invalid output aborts `submit` before any local, remote, or GitHub
change is made — so a broken helper cannot leave a half-updated stack.

A helper's output is description prose only. It never affects stack topology; `jj-stack` reads
that from the `jj` DAG on every run.

## Related

- [Daily Workflow](daily-workflow.md) — where description authoring fits in the normal loop
- `jj-stack submit --help` — the canonical flag reference
