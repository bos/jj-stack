# User guide

These pages are the canonical user documentation for `jj-stack`. The
[jj-stack website](https://www.serpentine.com/software/jj-stack/) publishes the same guides with
web navigation and presentation.

## Start here

- [Quick start](quick-start.md)
- [How jj-stack works](mental-model.md)

## Everyday work

- [Submit and update a stack](guides/submit-and-update.md)
- [Work with a stack on GitHub](guides/working-on-github.md)
- [Review and operate a stack](guides/review-a-stack.md)
- [Edit and rearrange a stack](guides/revise.md)
- [Merge and sync](guides/merge-and-sync.md)
- [Multiple stacks and dependent work](guides/multiple-stacks.md)
- [Continue an existing stack](guides/continue-a-stack.md)
- [Separate a stack or close pull requests](guides/close-or-separate.md)

## Reference and troubleshooting

- [Command reference](reference/commands.md)
- [Configuration](reference/configuration.md)
- [Pull request descriptions](reference/descriptions.md)
- [Automation and agents](reference/automation.md)
- [JSON output](reference/json-output.md)
- [Troubleshooting](troubleshooting.md)
- [`jj-stack` and `gh stack`](gh-stack.md)

The built-in help is the exact flag and alias reference:

```console
jj-stack --help
jj-stack <command> --help
jj-stack help --all
```

State-changing commands also accept `--dry-run` when you want to preview their work.
