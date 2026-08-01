# Exit Codes

`jj-stack` commands use a small set of process exit codes so scripts and agents can react
without parsing output. Where a meaning overlaps with the `gh stack` CLI extension, the code
matches; codes 7-9 are reserved because their `gh stack` meanings have no jj-stack analog.

| Code | Meaning |
|-----:|---------|
| 0 | Success. |
| 1 | Any other failure, including a command stopped by a blocked action. |
| 2 | The selection does not form a supported review stack. |
| 3 | Unresolved conflicts prevented a review update. |
| 4 | GitHub authentication, network, or API failure. |
| 5 | Invalid command-line arguments. |
| 6 | A selector matched more than one target, so the command failed closed. |
| 10 | `view` or `list` printed an incomplete report. |
| 130 | Interrupted. |

Notes:

- `view` and `list` are report commands. When they cannot inspect everything — GitHub is
  unreachable, a saved PR link has gone stale, a change has several visible revisions, or one
  selector among several fails to resolve — they still print the best report they can and exit
  10. Both commands apply the same rule, so they never disagree about whether one repository's
  report is complete. Exit 0 means the inspection
  completed; the report can still contain work to do, such as an orphaned PR or a stack that has
  changed since submit. When the command cannot produce a report at all, including when the only
  selector given fails to resolve, it fails with one of the error codes instead.
- With `--json`, exit 10 still comes with a valid payload on stdout; read the exit code
  together with the payload. See [json-output.md](json-output.md).
- Commands that mutate review state (`submit`, `merge`, `sync`, `unstack`, `cleanup`) exit 1 when
  they ran but had to stop before completing every action; command output names what blocked them.
- `sync` may finish its local rebase before exiting 3. Its message says whether to resolve the
  conflicts and continue with `submit`.
- Exit 2 covers selections `jj-stack` cannot review as a linear stack: a merge commit, a divergent
  change, a hidden or immutable commit, an empty or undescribed working copy, a path that never
  reaches `trunk()`, and a repository with no trunk bookmark configured. The message names the
  offending change where there is one; the trunk and working-copy cases have no change to name.
- Exit 6 means rerun with an explicit revision or repair an incorrect saved PR attachment with
  `relink`.
- `doctor` exits 0 when every check passed, warned, or was fixed, and 1 when any check failed. A
  non-zero `doctor` describes the repository, not a failure of the command itself.
