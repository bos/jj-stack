# User Guide

These pages explain how to turn a series of local `jj` changes into clear, dependent GitHub pull
requests while keeping history easy to revise.

- [Mental Model](mental-model.md)
  Understand what stays in `jj` and what `jj-stack` owns on GitHub.
- [Daily Workflow](daily-workflow.md)
  The normal author loop for submit, review, merge, sync, and cleanup.
- [Writing PR Descriptions](description-helpers.md)
  Overriding the derived title and body with files, an editor, or a helper program.
- [Troubleshooting](troubleshooting.md)
  Common symptoms, likely causes, and the next command to run.
- [JSON Output](json-output.md)
  The structured output schema for `view --json` and `list --json`.
- [Exit Codes](exit-codes.md)
  What each process exit code means for scripts and agents.

The repository [README](../README.md) is the canonical install and first-run
quickstart.

`jj-stack doctor` checks a repository's setup and GitHub access and names a fix
for anything it reports; `--fix` applies the local repairs it can make safely.
It is a good first command in a new clone and after any command that failed
unexpectedly.

Examples use angle brackets for values you must replace. For example, in
`jj-stack view <head-change-id>`, replace `<head-change-id>` with the change ID shown by `jj log`
or `jj-stack`.

In interactive terminals, longer multi-step GitHub work shows a progress bar
on stderr while `jj-stack` is waiting on GitHub.

The command-line help remains the canonical reference for flags and exact
parser behavior:

```bash
jj-stack --help
jj-stack help --all
jj-stack <command> --help
```

`jj-stack help --all` also shows command aliases where a command has one,
such as `status`, `st`, and `v` for `view`, `ls` for `list`, and `delete` for `unstack`.
