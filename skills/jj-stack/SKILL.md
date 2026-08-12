---
name: jj-stack
license: Apache-2.0
description: >
  Manage stacked GitHub review for jj with jj-stack. Use when inspecting,
  creating, submitting, refreshing, revising, merging, cleaning up, or
  recovering stacked pull requests for local jj changes, and before mutating
  any GitHub pull request or branch with gh or the GitHub API in a jj repo.
---

# jj-stack

`jj-stack` sends a linear chain of local `jj` changes to GitHub as dependent
pull requests. Division of labor: `jj` edits the local stack; `jj-stack` owns
its GitHub review state (review branches, PRs, merging, cleanup). Stable
`jj-stack/<subject-slug>-<eight-character-change-id>` branches stay on the
selected Git remote and do not become persistent local bookmarks. The
`jj-stack` prefix is the default; a repo may set `jj-stack.branch_prefix`.

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
   stack, and never create, delete, or force-push its review branches by
   hand. Closing a known pull request with GitHub or `gh pr close` is supported;
   use `jj-stack unstack` for GitHub stack grouping.
2. **Check tracking before the first `gh` or API write in a repo.** Run
   `jj-stack list --json`, or `jj-stack view --pull-request <pr> --json` for one PR. A matching
   PR or `branch` field proves tracking; a bare change with `status: unsubmitted` does not. Cache
   the answer for the session. Do this lazily — the trigger is a pending GitHub write, not
   entering a repo. These commands exit 10 when they print an incomplete report; read the JSON
   before concluding anything. If tracking is absent or ambiguous, stop and read
   [recovery workflows](references/recovery.md) before writing.
3. **Use jj-stack for stack changes.** Once jj-stack is detected anywhere
   in a repo, use it for stack-level PR work in that repo: status, submit,
   refresh, base/head changes caused by stack rewrites, merging, cleanup,
   importing, relinking, and recovery. `gh` remains fine for reads, closing
   known pull requests, and
   collaboration metadata, but not for deciding or changing stack shape.
4. **Inspect before mutating.** Run `view` or `list` before `submit`, `merge`,
   `cleanup`, or `unstack`, and preview with `--dry-run` whenever the next
   step is uncertain.
5. **Select explicitly after anything ambiguous.** `submit` defaults to the
   stack ending at `@` when the working-copy change is described and nonempty,
   otherwise `@-`. After an interrupted command, or in a
   multi-stack repo, pass a change ID, revset, or `--pull-request` selector.
   Prefer change IDs in user-facing summaries; use commit IDs only when a
   concrete immutable snapshot matters.
6. **Stay non-interactive.** Do not use `submit --edit`, `checkout --pick`, or
   an interactive `--describe-with` helper; those open an editor or prompt on
   stdin for humans. Pass `--describe` files and explicit selectors instead.

## Load references when needed

- Read [multi-stack workflows](references/multi-stack.md) before acting on a forked local DAG,
  a child review based on another review, a move between stacks, a split or join, or a command
  that would change more than one GitHub stack.
- Read [recovery workflows](references/recovery.md) after an interrupted or externally completed
  operation, a direct structural GitHub mutation, lost or ambiguous tracking, an orphaned review,
  a GitHub grouping mismatch, or any task involving `sync --all`, `unstack --stack`, `checkout`,
  `relink`, or starting reviews over.

## Using `gh` on a managed stack

**Reads are always fine**: `gh pr view`, `gh pr list`, `gh pr checks`,
`gh pr diff`, and other read-only queries.

**Collaboration writes are fine when the user asks**: comments, reviews,
labels, assignees, milestones, reviewer requests, draft/ready state, and
title or body edits (a later `submit` may overwrite generated title/body
text). Never edit or delete comments containing `<!-- jj-stack-overview -->`;
jj-stack manages those.

**Closing and reopening known pull requests is supported when the user asks.**
Inspect the stack first, use explicit PR numbers, and leave jj-stack's saved
links in place so `cleanup` can verify what it removes. Remove GitHub stack
grouping with `jj-stack unstack` before closing a whole stack.

**Other structural and lifecycle writes are not**: merging a PR; retargeting
base or head; deleting or force-pushing a review branch; creating a replacement
PR; changing GitHub stack membership outside `jj-stack`; or equivalent `gh api`
mutations. These desync local changes, review branches, and tracking data. Map
the intent to a jj-stack command instead; use `gh` only if the user explicitly
confirms after you explain that risk.

## Everyday flow

1. Build or revise the stack with `jj`. Each change is one reviewable PR:
   put a dependency in the same change or a lower one, and unrelated work in
   a separate stack.
2. Confirm the shape with `view` (`--json` for machine-readable output; it
   reads GitHub but does not fetch, so run `jj git fetch` first when local
   trunk may be behind); `list` shows the repo-wide inventory.
3. `submit --dry-run`, then `submit` to create or refresh PRs. Add
   `--re-request` only when the user wants previous reviewers asked again.
4. Apply review feedback in the change it belongs to: edit the lower `jj`
   change, let descendants rebase, then `view` and `submit`. Do not patch a
   higher change to avoid touching a lower one.
5. When bottom changes are ready, run `merge --dry-run`, then `merge`. It selects
   consecutive open, non-draft PRs from the bottom and requires their exact submitted commits.
   GitHub decides approvals, checks, conflicts, and repository policy. A completed direct merge
   updates the local stack automatically; it never pushes trunk. After a queued merge, run
   `sync <head-change-id>` once GitHub finishes.
6. If `trunk()` merely advanced, use plain `jj rebase`. `sync` is for
   ancestors already merged on GitHub under exact or rewritten commit IDs.

## Closing and cleanup

To close a stack without merging, inspect it, run `unstack --dry-run <head-change-id>` and
`unstack <head-change-id>`, then close each explicit PR with `gh pr close <pr>`. Preserve saved
links until `cleanup --dry-run <head-change-id>` and `cleanup <head-change-id>` verify and remove
the closed stack's branches, managed comments, and tracking.

Run `cleanup --dry-run`, then `cleanup`, to collect eligible closed or already-synced merged
leftovers across the repository. Open reviews, open orphans, mismatched identities, unavailable
GitHub state, and branches still needed as PR bases remain untouched.

## Exit codes

0 success; 1 any other failure, including a blocked action; 2 selection is
not a supported stack; 3 unresolved conflicts; 4 GitHub auth/API failure;
5 invalid arguments; 6 ambiguous selector (fails closed — use `relink` to
repair an incorrect attachment or select explicitly); 10 `view`/`list` printed a report
that is incomplete or needs attention (the output is still valid — read it);
130 interrupted.

## When something goes wrong

Stop on ambiguity and ask for a concrete selector. Use `jj workspace update-stale` for a stale
workspace and `jj op log` or `jj undo` for local recovery; never use destructive Git commands.
For every other abnormal lifecycle state, read [recovery workflows](references/recovery.md)
before acting.
