# Backlog

Keep only concrete, non-blocking work with a plausible user benefit. Completed work belongs in
commit history, and speculative tests or optimizations should be added only after an observed
failure or measured cost.

## Sync retirement can strand review artifacts

_Benefit: high — merged reviews can leave branches in the reserved namespace with no supported
way to remove them._

Both selected `sync` and `sync --all` can remove tracking for a merged review without deleting its
review branch or managed comment. `cleanup` then lacks the saved identity and submitted commit it
needs to verify those artifacts, so it reports that no cleanup is needed.

This has been reproduced on a real repository with a merged single-change review. The branch had
to be deleted outside `jj-stack`.

Decide which command owns artifact deletion:

- `sync` deletes verified artifacts before it removes tracking, or
- `sync` keeps tracking until `cleanup` deletes the artifacts.

Apply the same rule to selected and repository-wide sync. Then update [design.md](design.md), the
user guide, troubleshooting guidance, and tests in the same change.
