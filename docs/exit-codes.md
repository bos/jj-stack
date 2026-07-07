# Exit Codes

`jj-stack` commands use a small set of process exit codes so scripts and agents can react
without parsing output. Where a meaning overlaps with the `gh stack` CLI extension, the code
matches; codes 7-9 are reserved because their `gh stack` meanings have no jj-stack analog.

| Code | Meaning |
|-----:|---------|
| 0 | Success. |
| 1 | Any other failure, including a command stopped by a blocked action. |
| 2 | The selection does not form a supported review stack. |
| 3 | Unresolved conflicts in the selected changes block the operation. |
| 4 | GitHub authentication, network, or API failure. |
| 5 | Invalid command-line arguments. |
| 6 | A selector matched more than one target, so the command failed closed. |
| 10 | `view` or `list` printed a report that is incomplete or needs attention. |
| 130 | Interrupted. |

Notes:

- `view` and `list` are report commands. When they cannot inspect everything — GitHub is
  unreachable, a saved PR link has gone stale, or one selector fails to resolve — they still
  print the best report they can and exit 10. Exit 0 from `view` or `list` means the report
  is complete and healthy. When they cannot produce a report at all, they fail with one of
  the error codes instead.
- With `--json`, exit 10 still comes with a valid payload on stdout; read the exit code
  together with the payload. See [json-output.md](json-output.md).
- Commands that mutate review state (`submit`, `land`, `unstack`, `cleanup`) exit 1 when they
  ran but had to stop before completing every action; stderr names what blocked them.
- Exit 2 covers stack shapes `jj-stack` does not review: merge commits, divergent changes, a
  working copy that never reaches `trunk()`, and similar. The message names the offending
  change.
- Exit 6 means repair the selection first, for example with `unlink` or `relink`, or rerun
  with an explicit revision.
