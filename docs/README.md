# User Guide

These pages are the user-facing guide set for `jj-stack`.

One recurring term in the docs is the review bookmark. By default that
bookmark looks like `review/...`, but a repo can configure a different
prefix. It is the user-visible local `jj` bookmark `jj-stack` uses as the
GitHub head branch for one review change.

- [Mental Model](mental-model.md)
  Understand what stays in `jj` and what `jj-stack` owns on GitHub.
- [Daily Workflow](daily-workflow.md)
  The normal author loop for submit, review, land, and cleanup.
- [Troubleshooting](troubleshooting.md)
  Common symptoms, likely causes, and the next command to run.

The repository [README](../README.md) is the canonical install and first-run
quickstart.

In interactive terminals, longer multi-step GitHub work shows a progress bar
on stderr while `jj-stack` is waiting on GitHub.

The command-line help remains the canonical reference for flags and exact
parser behavior:

```bash
jj-stack --help
jj-stack help --all
jj-stack <command> --help
```

`jj-stack help --all` also shows short command aliases where a command has one,
such as `ls` for `list` and `delete` for `unstack`.
