python := if os() == "windows" { ".venv/Scripts/python.exe" } else { ".venv/bin/python" }

alias fmt := format
alias web := website

# List the available development workflows.
default:
    @just --list

# Install the locked development environment.
setup:
    uv sync --locked

# Run jj-stack from this checkout.
[positional-arguments]
run *args:
    uv run jj-stack "$@"

# Refresh the sibling website's generated jj-stack documentation snapshot.
website:
    cd ../website && JJ_STACK_SOURCE=../jj-stack scripts/sync-jj-stack-docs.py

# Check whether the sibling website's jj-stack documentation snapshot is current.
website-check:
    cd ../website && JJ_STACK_SOURCE=../jj-stack scripts/sync-jj-stack-docs.py --check

# Apply the repository's Ruff fixes and formatting.
format: setup
    {{python}} -m ruff check --fix
    {{python}} -m ruff format

# Run the standard Ruff, type-check, and test pass.
check *args:
    ./check.py {{args}}

# Run all tests in parallel by default, or pass a focused pytest selection and options.
test *args='-n auto': setup
    {{python}} -m pytest {{args}}

# Check the cumulative code and test complexity budgets.
complexity:
    uv run tools/check_complexity.py

# Run generated client and server command sequences; arguments pass through to the runner.
[positional-arguments]
property *args:
    tests/run_submit_property_scenarios.py "$@"

# Run the opt-in release checks against a disposable GitHub repository.
live *args:
    uv run python tests/run_live_github.py {{args}}

# Build the wheel and source distribution.
build:
    rm -f dist/jj_stack-*.whl dist/jj_stack-*.tar.gz
    uv build

# Build and smoke-test both release artifacts outside the source tree.
artifact-check: build
    uv run --no-project --python 3.14 python tools/check_release_artifacts.py

# Run all local release qualification gates.
release-check: check complexity live
