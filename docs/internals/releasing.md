# Release process

A release is built from a `v<version>`-tagged commit on `main`. The tag workflow builds and tests
both distributions, publishes them to PyPI, and creates the matching GitHub Release. A manual
workflow run publishes to TestPyPI unless it names an existing release tag to retry.

## Qualify the candidate

Set the intended version in `pyproject.toml`, finish the release changes, and run the release
gates:

```console
./check.py
uv run tools/check_complexity.py
uv run python tests/run_live_github.py
```

The live test requires a `gh` login that can create and delete a private repo, push to it, and
manage its pull requests.

Check the website snapshot and production build:

```console
cd ../website
JJ_STACK_SOURCE=../jj-stack scripts/sync-jj-stack-docs.py --check
just check
```

If the snapshot is out of date, run `scripts/sync-jj-stack-docs.py`, review and commit the website
change, then rerun the checks. Push the release changes to `main` before creating the tag. A
manual run of the release workflow is the optional TestPyPI smoke test.

## Publish the tag

Create the version tag and push it explicitly:

```console
jj tag set v0.1.0 -r main
jj git push --tag v0.1.0
```

Use the version from `pyproject.toml` in place of `0.1.0`. After the workflow succeeds, verify the
package and release pages, publish the already-checked website with `just publish`, and verify the
live quick start and install command.

Never move or reuse a published version tag. A failed workflow can be rerun against the same tag;
a source change requires a new version and tag.
