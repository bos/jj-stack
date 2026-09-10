# User guide

Use these guides to submit, revise, review, and merge stacked GitHub pull requests with `jj`.
You can also read them on the [jj-stack website](https://www.serpentine.com/software/jj-stack/).

## Start here

- [Quick start](quick-start.md)
- [How jj-stack works](mental-model.md)

## Everyday work

- [Submit and update a stack](guides/submit-and-update.md)
- [Edit and rearrange a stack](guides/revise.md)
- [Work with a stack on GitHub](guides/working-on-github.md)
- [Review and merge a stack on GitHub](guides/review-a-stack.md)
- [Merge and sync](guides/merge-and-sync.md)
- [Multiple stacks and dependent work](guides/multiple-stacks.md)
- [Continue an existing stack](guides/continue-a-stack.md)
- [Separate a stack or close pull requests](guides/close-or-separate.md)

## Reference and troubleshooting

- [Command reference](reference/commands.md)
- [Bookmarks and stack selection](reference/bookmarks-and-selection.md)
- [Configuration](reference/configuration.md)
- [Pull request descriptions](reference/descriptions.md)
- [Automation and agents](reference/automation.md)
- [JSON output](reference/json-output.md)
- [Troubleshooting](troubleshooting.md)
- [Compare `jj-stack` with other tools](tool-comparison.md)

For all flags and aliases, use the built-in help:

```console
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

`submit`, `merge`, `unstack`, `cleanup`, and `sync` accept `--dry-run` when you want to preview
their work.
