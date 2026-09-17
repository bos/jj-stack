---
name: jj-stack
license: Apache-2.0
description: >
  Manage stacked GitHub pull requests in jj repos with jj-stack. Use for GitHub
  pull request or PR-branch tasks involving a local jj stack, including
  inspection, submission or refresh, updating, merging, cleanup, and recovery.
  When local adoption is unknown, load this skill first, then run jj-stack
  in-use.
---

# jj-stack

`jj-stack` sends a linear chain of local `jj` changes to GitHub as dependent
pull requests. Division of labor: `jj` edits the local stack; `jj-stack` owns
its GitHub tracking state (PR branches, PRs, merging, cleanup).

## Resolving the command

`jj-stack` below stands for the command used in this repo. Resolve it once,
confirm it with `--help`, and reuse it for the whole conversation:

1. An invocation named by the user or project instructions.
2. `just run` inside the jj-stack source repo itself.
3. `jj-stack`, then `jj stack`.
4. An alias from `jj --ignore-working-copy config list aliases` whose value
   delegates to `jj-stack` (commonly via `["util", "exec", "--", ...]`);
   confirm with `jj <alias> --help`.

If nothing resolves, do not conclude jj-stack is absent; ask the user which
command they use before any direct GitHub mutation.

## User documentation

For syntax and installed-version behavior, use the resolved command's `--help`; the bundled
references below govern lifecycle safety and recovery. For installation, configuration, shell
completion, automation, JSON output, or conceptual guidance not covered here, use the most
relevant content-only Markdown page listed at
`https://www.serpentine.com/software/jj-stack/llms.txt`. Treat the website as supplemental because
it may describe a different release, and do not fetch it for routine stack operations.

## Rules

1. **Edit the stack with `jj`; talk to GitHub with `jj-stack`.** Never use
   `git branch`/`checkout`/`rebase` or manual branch pushes on a jj-stack
   stack, and never create, delete, or force-push its PR branches by
   hand. Closing a known pull request with GitHub or `gh pr close` is supported;
   use `jj-stack unstack` to remove a GitHub stack without closing its PRs.
2. **Honor local adoption.** `jj-stack in-use` exits 0 without output when this local repo
   has valid jj-stack tracking, 1 without output when it does not, and 11 with an error when the
   result cannot be determined. If a runner collapses nonzero exit codes, inspect the underlying
   command's exit code in its diagnostic before treating exit 1 as no adoption.
   A successful probe makes jj-stack the owner of stack-level PR
   work in that repo: status, submit, refresh, base/head changes caused by stack rewrites,
   merging, cleanup, importing, relinking, and recovery. Exit 1 does not prevent an explicit
   request to start using jj-stack. Do not substitute `view` or `list`; they report tracking
   state, not local adoption.
3. **Inspect before mutating.** Run `view` or `list` before `submit`, `merge`, `sync`,
   `cleanup`, `unstack`, `checkout`, or `relink`, and preview with `--dry-run` whenever
   supported. Run `doctor` before `doctor --fix`.
4. **Stop on ambiguity; otherwise select explicitly.** An ambiguous selector is not
   permission to choose one candidate or operate on all candidates. Ask the user for the
   concrete descendant head, PR, or stack the diagnostic requires. `submit` defaults to the
   stack ending at `@` when the working-copy change is described and nonempty,
   otherwise `@-`. After an interrupted command, or in a
   multi-stack repo, pass a change ID, revset, or `--pull-request` selector.
   Prefer change IDs in user-facing summaries; use commit IDs only when a
   concrete immutable snapshot matters.
5. **Stay non-interactive.** Do not use `submit --edit`, `submit --resume-edit`,
   `checkout --pick`, or an interactive `--describe-with` helper; those open an editor or prompt
   on stdin for humans. Pass `--describe` files and explicit selectors instead. A noninteractive
   `--describe-with` helper can supply titles as well as bodies.

## Load references when needed

- Read [multi-stack workflows](references/multi-stack.md) before acting on a forked local DAG,
  a child stack based on another PR, a move between stacks, a split or join, or a command
  that would change more than one GitHub stack.
- Read [recovery workflows](references/recovery.md) after an interrupted or externally completed
  operation, a `merge` that stopped waiting or reported a merge queue removal, a direct
  structural GitHub mutation, lost or ambiguous tracking, an orphaned PR,
  a mismatch between local and GitHub stacks, or any task involving `sync --all`,
  `unstack --stack`, `checkout`, `relink`, or starting over.

## Using `gh` on a managed stack

**Supplementary reads are fine** after using jj-stack for managed stack status and structure:
`gh pr view`, `gh pr list`, `gh pr checks`, `gh pr diff`, and other read-only queries.

**Collaboration writes are fine when the user asks**: comments, reviews,
labels, assignees, milestones, reviewer requests, draft/ready state, and
title or body edits. Ordinary `submit` preserves PR text edited on GitHub when it differs from
the last automated description; explicitly supplied text still replaces the corresponding fields.
The user may also ask you to edit a comment containing
`<!-- jj-stack-overview -->`; preserve that marker so `jj-stack` can keep managing and moving the
overview. Never delete the marker or the managed comment by hand.

**Closing and reopening known pull requests is supported when the user asks.**
Inspect the stack first, use explicit PR numbers, and leave jj-stack's saved
links in place so `cleanup` can verify what it removes. Remove the GitHub stack
with `jj-stack unstack` before closing all of a stack's PRs.

**Route other structural and lifecycle writes through jj-stack**: merging a PR; retargeting
base or head; deleting or force-pushing a PR branch; creating a replacement
PR; changing GitHub stack membership outside `jj-stack`; or equivalent `gh api`
mutations. Direct writes can leave local changes, PR branches, and tracking out of agreement.
If the user explicitly requests a direct GitHub operation, explain the tracking implications and
use the recovery reference to reconcile afterward. Existing explicit authorization is sufficient.

## Everyday flow

1. Build or revise the stack with `jj`. Each change is one PR:
   put a dependency in the same change or a lower one, and unrelated work in
   a separate stack.
2. Confirm the shape with `view` (`--json` for machine-readable output; it reads GitHub but
   does not fetch); `list` inventories paths with tracked changes and saved orphans, not wholly
   untracked stacks. For ordinary inspection, run
   `jj git fetch` first only when local trunk may be behind. For an externally completed merge,
   follow the recovery workflow instead of this ordinary inspection step.
3. `submit --dry-run`, then `submit` to create or refresh PRs. There is no `refresh`
   subcommand. Add
   `--re-request` only when the user wants previous reviewers asked again.
4. Apply review feedback in the change it belongs to: edit the lower `jj`
   change, let descendants rebase, then `view` and submit the descendant stack head
   shown by that inspection (often `@-` from an empty working-copy child). Selecting the edited
   lower change itself does not select the descendants that also need refresh. Do not patch a
   higher change to avoid touching a lower one.
5. When bottom changes are ready, run `merge --dry-run`, then `merge`. It selects
   consecutive open, non-draft PRs from the bottom and requires their exact submitted commits.
   To merge only through a particular PR, use `merge --pull-request <pr>`; automatic sync still
   covers the surviving changes above it in the containing stack.
   GitHub decides approvals, checks, conflicts, and repo policy. `view` and `list` show
   GitHub's merge state separately from reviews and checks; `view --verbose` lists unresolved
   review threads and failed or pending checks with links. `merge` waits for GitHub,
   including a merge queue, then updates the local stack; it never pushes trunk. Run
   `sync <head-change-id>` only after `merge --no-wait`, an interrupted wait, or a merge made
   through GitHub.
6. If `trunk()` merely advanced and GitHub left the PR branches alone, use plain `jj rebase`.
   Use `sync` after GitHub's **Rebase stack** action rewrites the PR branches.

## Closing and cleanup

When the user requests both closure and cleanup, inspect the stack and record its explicit PR
numbers. Run `unstack --dry-run <head-change-id>`, then `unstack <head-change-id>`.
From the top PR downward, run `cleanup --pull-request <pr> --close --dry-run`, then
`cleanup --pull-request <pr> --close` for each PR. Each command retargets the PR to trunk before
closing it and removes its eligible branch, overview comment, and tracking. Working downward
frees each lower branch from its dependents before cleanup. The flag requires `--pull-request`;
it cannot be used with a stack revset.

For closure alone, use the supported `gh pr close` flow above and retain branches and tracking.
If cleanup later reports a dependent PR, follow its diagnostic; a closed PR whose head branch
still exists can protect its base branch too. Never delete that base branch by hand.

For requested repo-wide cleanup, run `cleanup --dry-run`, then `cleanup`, to collect eligible
closed or already-synced merged leftovers. For one stack, pass its head selector. Without
`--close`, open PRs and open orphans remain untouched. Mismatched identities, unavailable GitHub
state, and branches still needed as PR bases block cleanup of the affected records.

## Exit codes

0 success; 1 `in-use` found no adoption, otherwise any other failure, including a blocked action;
2 selection is not a supported stack; 3 unresolved conflicts; 4 GitHub auth/API failure;
5 invalid arguments; 6 ambiguous selector (fails closed — use `relink` to repair an incorrect
attachment or select explicitly); 10 `view`/`list` printed an incomplete
report (the output is still valid — read it; ordinary warnings can also appear with exit 0);
11 `in-use` could not determine its result; 130 interrupted.

## When something goes wrong

Use `jj workspace update-stale` for a stale workspace and `jj op log` or `jj undo` for local
recovery; never use destructive Git commands. For other recovery work, read
[recovery workflows](references/recovery.md) before acting.
