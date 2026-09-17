# Release process

A release is built from a `v<version>` tag on a commit reachable from `main`. The
[release workflow](../../.github/workflows/release.yml) verifies the tag and notes, runs local
tests, builds and tests both distributions, publishes to PyPI, then creates the GitHub Release.
A manual run without `release_tag` publishes to TestPyPI; naming an existing release tag retries
production publication.

Run the commands below from the jj-stack checkout unless a subshell explicitly changes directory.

## Write the release notes

Create `release-notes/v<version>.md` with the version from `pyproject.toml` and include it in the
tagged commit. The production workflow rejects a missing or empty file before publication. The
file supplies the GitHub Release body.

Read the changes since the previous tag and the affected user docs. Use commit history to find
candidates, then organize the notes around reasons to upgrade or actions users must take:

- New workflows or capabilities.
- Changes to installation requirements, commands, configuration, or public output formats.
- Fixes for recognizable symptoms, especially blocked recovery or risk of lost work.
- Substantial guides that help users complete a workflow.

Omit internal refactors, dependencies, tests, CI, and routine hardening unless their user-visible
consequence matters. Combine commits that solve the same problem into one outcome.

### Structure and wording

Open with one or two sentences naming the most useful changes. Group entries under headings such
as `Breaking changes`, `Highlights`, `Fixes`, and `Documentation`, omitting empty sections. Put
breaking changes first: identify the affected users, what stops working, and the migration or
workaround. End with a `Full changelog` link comparing the previous and new tags.

Each bullet should describe one outcome a user can recognize. Explain the old symptom when it
helps show why a fix matters, and include a command when the reader must act. Keep entries
self-contained even when they link to an issue or PR. Do not paste commit subjects or list
implementation mechanisms.

Use the vocabulary of the user guides. For example, replace “`sync` detects moved survivor
branches before rewriting local history” with “If a remaining PR branch changed on GitHub, `sync`
now stops before rebasing your local changes and tells you how to recover.” Name public commands,
options, configuration keys, and JSON values when they help the reader act.

Delete an entry if it does not make clear who benefits or what they must do. Fact-check claims
against the released behavior, verify commands safely or against `--help`, and check links and the
rendered Markdown before tagging.

Do not hard-wrap release-note prose: each paragraph or list item stays on one physical line.
GitHub preserves source line breaks in Release bodies. This is an exception to the repo's usual
98-column wrap.

## Qualify the candidate

Set the version in `pyproject.toml` and finish the release changes and notes. Pyrefly and ty
must both pass before publishing a release. Run both type checkers and both local gates:

```console
just check -t pyrefly -t ty
just release-check
just artifact-check
```

The combined check stops at the first failure. Resolve the reported type errors and rerun it
until both checkers pass. `just release-check` alone runs only the default Pyrefly checks.

`release-check` runs the standard checks, complexity checker, and live GitHub suite. It requires
a `tokei` version supported by [the complexity checker](../../tools/check_complexity.py), plus a
`gh` login that can create and delete a private repo, push to it, and manage its PRs. The live
runner creates a disposable repo and attempts deletion even on failure; retain its output to
identify any cleanup failure. `just live --help` describes runner options.

`artifact-check` builds the wheel and source distribution and smoke-tests both outside the source
tree. It is a separate recipe; `release-check` does not include it. CI repeats the artifact checks
before publishing.

Check the website snapshot and production build:

```console
just website-check
(cd ../website && just check)
```

If the snapshot is stale, run `just website`, review and commit the website update, then rerun
these checks. Push the release changes to `main` before tagging. A manual release workflow run
without `release_tag` is an optional TestPyPI smoke test; it publishes packages, so use a version
that TestPyPI will accept.

## Publish the tag

From the jj-stack checkout, tag the checked release commit and push the tag explicitly:

```console
jj tag set v0.1.3 -r main
jj git push --tag v0.1.3
```

Replace `0.1.3` with the intended version. Confirm that `main` names the checked commit before
running these commands. After the workflow succeeds, verify the GitHub Release has the authored
notes and downloadable distributions, and that the PyPI package page links to GitHub Releases
through `Changelog`.

Publish the checked website, then verify its live quick start and install command:

```console
(cd ../website && just publish)
```

## Retry a failed publication

Never move a published tag or reuse a published version for changed source. Rerun the workflow
against the same tag, or dispatch it with that `release_tag`. A source change requires a new
version and tag.

Publication is sequential: a GitHub Release failure can occur after PyPI has accepted the
packages. When a GitHub Release already exists, the final job compares its body with the tagged
notes and verifies its assets rather than overwriting them. A mismatch fails that job; it does
not undo PyPI publication. Inspect the failed job before retrying.
