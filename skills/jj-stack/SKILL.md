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

`jj-stack` below is a placeholder for the real invocation. Resolve one per
repo, confirm it with `--help`, and reuse it for the whole conversation:

1. An invocation named by the user or project instructions.
2. `uv run jj-stack` inside the jj-stack source repo itself.
3. `jj-stack`, then `jj stack`.
4. An alias from `jj --ignore-working-copy config list aliases` whose value
   delegates to `jj-stack` (commonly via `["util", "exec", "--", ...]`);
   confirm with `jj <alias> --help`.

If nothing resolves, do not conclude jj-stack is absent; ask the user which
command they use before any direct GitHub mutation.

## Rules

1. **Edit the stack with `jj`; talk to GitHub with `jj-stack`.** Never use
   `git branch`/`checkout`/`rebase` or manual branch pushes on a jj-stack
   stack, and never create, delete, or force-push its PR branches by
   hand. Closing a known pull request with GitHub or `gh pr close` is supported;
   use `jj-stack unstack` for GitHub stack grouping.
2. **Honor local adoption.** `jj-stack in-use` exits 0 without output when this local repo
   has valid jj-stack tracking, 1 without output when it does not, and 11 with an error when the
   result cannot be determined. A successful probe makes jj-stack the owner of stack-level PR
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
5. **Stay non-interactive.** Do not use `submit --edit`, `checkout --pick`, or
   an interactive `--describe-with` helper; those open an editor or prompt on
   stdin for humans. Pass `--describe` files and explicit selectors instead.

## Load references when needed

- Read [multi-stack workflows](references/multi-stack.md) before acting on a forked local DAG,
  a child stack based on another PR, a move between stacks, a split or join, or a command
  that would change more than one GitHub stack.
- Read [recovery workflows](references/recovery.md) after an interrupted or externally completed
  operation, a direct structural GitHub mutation, lost or ambiguous tracking, an orphaned PR,
  a GitHub grouping mismatch, or any task involving `sync --all`, `unstack --stack`, `checkout`,
  `relink`, or starting over.

## Using `gh` on a managed stack

**Supplementary reads are fine** after using jj-stack for managed stack status and structure:
`gh pr view`, `gh pr list`, `gh pr checks`, `gh pr diff`, and other read-only queries.

**Collaboration writes are fine when the user asks**: comments, reviews,
labels, assignees, milestones, reviewer requests, draft/ready state, and
title or body edits (a later `submit` may overwrite generated title/body
text). Never edit or delete comments containing `<!-- jj-stack-overview -->`;
jj-stack manages those.

**Closing and reopening known pull requests is supported when the user asks.**
Inspect the stack first, use explicit PR numbers, and leave jj-stack's saved
links in place so `cleanup` can verify what it removes. Remove GitHub stack
grouping with `jj-stack unstack` before closing all of a stack's PRs.

**Other structural and lifecycle writes are not**: merging a PR; retargeting
base or head; deleting or force-pushing a PR branch; creating a replacement
PR; changing GitHub stack membership outside `jj-stack`; or equivalent `gh api`
mutations. These desync local changes, PR branches, and tracking data. Map
the intent to a jj-stack command instead; use `gh` only if the user explicitly
confirms after you explain that risk.

## Everyday flow

1. Build or revise the stack with `jj`. Each change is one PR:
   put a dependency in the same change or a lower one, and unrelated work in
   a separate stack.
2. Confirm the shape with `view` (`--json` for machine-readable output; it reads GitHub but
   does not fetch); `list` shows the repo-wide inventory. For ordinary inspection, run
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
   GitHub decides approvals, checks, conflicts, and repo policy. A completed direct merge
   updates the local stack automatically; it never pushes trunk. After a queued merge, run
   `sync <head-change-id>` once GitHub finishes.
6. If `trunk()` merely advanced, use plain `jj rebase`. `sync` is for
   ancestors already merged on GitHub under exact or rewritten commit IDs.

## Closing and cleanup

To close the PRs in a stack without merging, inspect it, run
`unstack --dry-run <head-change-id>` and
`unstack <head-change-id>`, then close each explicit PR with `gh pr close <pr>`. Preserve saved
links until `cleanup --dry-run <head-change-id>` and `cleanup <head-change-id>` verify and remove
the closed PRs' branches, managed comments, and tracking.

Run `cleanup --dry-run`, then `cleanup`, to collect eligible closed or already-synced merged
leftovers across the repo. Open PRs, open orphans, mismatched identities, unavailable
GitHub state, and branches still needed as PR bases remain untouched.

## Exit codes

0 success; 1 any other failure, including a blocked action; 2 selection is
not a supported stack; 3 unresolved conflicts; 4 GitHub auth/API failure;
5 invalid arguments; 6 ambiguous selector (fails closed — use `relink` to
repair an incorrect attachment or select explicitly); 10 `view`/`list` printed a report
that is incomplete or needs attention (the output is still valid — read it); 11 `in-use`
could not determine its result;
130 interrupted.

## When something goes wrong

Use `jj workspace update-stale` for a stale workspace and `jj op log` or `jj undo` for local
recovery; never use destructive Git commands. For every other abnormal lifecycle state, read
[recovery workflows](references/recovery.md) before acting.
