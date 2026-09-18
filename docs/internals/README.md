# Contributor reference

Start with [CONTRIBUTING.md](../../CONTRIBUTING.md) for setup and commands, and
[AGENTS.md](../../AGENTS.md) for the repo's working agreement. For using the tool, see the
[user guide](../README.md).

Each internal document answers a different question:

- [Design](design.md): What does the product promise, and what must it refuse? Read before
  changing behavior, starting with the summary, core concepts, and safety rules.
- [Testing philosophy](testing-philosophy.md): Which risks deserve tests, and at which layer?
  Read before changing tests or fixtures.
- [Generated integration testing](property-testing.md): Why does the harness separate certain
  actions, restrict file edits, and isolate saved examples?
- [Code reviews](code-reviews.md): What should a reviewer look for in code, tests, and docs?
- [Releasing](releasing.md): How are release notes written, candidates checked, and versions
  published?

Keep rules in the relevant source above and link to them from other documents. These files
describe current behavior and rationale; commit history records how the design changed.

## Finding the code

- [cli.py](../../src/jj_stack/cli.py) parses arguments and dispatches commands.
  [bootstrap.py](../../src/jj_stack/bootstrap.py) resolves the repo and builds command context.
- For submission, start at
  [submit/command.py](../../src/jj_stack/commands/submit/command.py). It prepares the selected
  changes, observes GitHub, and resolves descriptions. Then
  [publication.py](../../src/jj_stack/commands/submit/publication.py) plans PR updates and stack
  membership, renders a preview or applies the plan, and reports completed publication.
- For status, [view.py](../../src/jj_stack/commands/view.py) selects local stacks and
  [list_.py](../../src/jj_stack/commands/list_.py) discovers tracked paths and orphaned PRs.
  [status.py](../../src/jj_stack/stack/status.py) combines local changes with GitHub observations;
  [change_state.py](../../src/jj_stack/stack/change_state.py) classifies them, and
  [reporting.py](../../src/jj_stack/stack/reporting.py) derives display status and the
  `needs_submit`/`needs_sync` fields.
- For sync, [sync.py](../../src/jj_stack/commands/sync.py) gathers observations, with additional
  GitHub reads in
  [convergence_observation.py](../../src/jj_stack/stack/convergence_observation.py).
  [convergence.py](../../src/jj_stack/stack/convergence.py) plans the selected stack's updates;
  [sync_apply.py](../../src/jj_stack/commands/sync_apply.py) applies local changes, refreshes PRs,
  and runs cleanup.
- [jj/client.py](../../src/jj_stack/jj/client.py) provides jj queries and mutations;
  [github/client.py](../../src/jj_stack/github/client.py) handles GitHub requests.
  [state/store.py](../../src/jj_stack/state/store.py) reads and writes tracking data.
- [timing.py](../../src/jj_stack/timing.py) records the per-call durations that `--time-output`
  reports. Start a performance investigation by running the slow command with that flag.
