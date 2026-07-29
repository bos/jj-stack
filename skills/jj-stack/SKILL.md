---
name: jj-stack
license: Apache-2.0
description: >
  Manage jj-native stacked GitHub review with jj-stack. Use when inspecting,
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
   hand. Use `gh stack` only for the exact resource-dissolution repair
   described below.
2. **Check tracking before the first `gh` or API write in a repo.** Run
   `jj-stack list --json`, or `jj-stack view --pull-request <pr> --json`
   for one PR. A matching PR or `branch` field proves tracking; a bare
   change with `status: unsubmitted` does not. Absence does not prove a GitHub
   PR is unmanaged after local tracking loss: run
   `checkout --pull-request <pr> --fetch` and inspect again. Cache the answer
   for the session. Do this lazily — the trigger is a pending GitHub write, not
   entering a repo. These commands exit 10 when they print a report that is
   incomplete or needs attention; read the JSON before concluding anything.
3. **Use jj-stack for stack changes.** Once jj-stack is detected anywhere
   in a repo, use it for stack-level PR work in that repo: status, submit,
   refresh, base/head changes caused by stack rewrites, merging, cleanup,
   closing, importing, relinking, and recovery. `gh` remains fine for reads and
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

## Using `gh` on a managed stack

**Reads are always fine**: `gh pr view`, `gh pr list`, `gh pr checks`,
`gh pr diff`, and other read-only queries.

**Collaboration writes are fine when the user asks**: comments, reviews,
labels, assignees, milestones, reviewer requests, draft/ready state, and
title or body edits (a later `submit` may overwrite generated title/body
text). Never edit or delete comments containing `<!-- jj-stack-navigation -->`
or `<!-- jj-stack-overview -->`; jj-stack manages those.

**Structural and lifecycle writes are not**: closing, merging, or reopening a
PR; retargeting base or head; deleting or force-pushing a review branch;
creating a replacement PR; or equivalent `gh api` mutations. These desync
local changes, review branches, and tracking data. Map the intent to a
jj-stack command instead; use `gh` only if the user explicitly confirms after
you explain that risk. The one routine exception is an exact `gh stack
unstack <number>` command printed by `submit` when one old GitHub stack spans
multiple desired local paths; run it, then submit each path separately.

- **Merge reviewed bottom changes:** `merge --dry-run`, then `merge`. It
  selects the consecutive open, non-draft PRs from the bottom and requires
  every candidate to match the exact submitted commit. GitHub decides
  approvals, checks, conflicts, and repository policy. Repositories with
  GitHub stack support use one atomic bottom-prefix request; others merge PRs
  bottom-up and may stop after lower PRs have merged. It never pushes trunk or
  rewrites local history. Run the selected `sync <head-change-id>` printed
  after GitHub accepts anything.
- **Close an abandoned stack's PRs:** `unstack --dry-run`, then `unstack`.
- **Also remove review branches and tracking:** `unstack --cleanup`, only
  after confirming the stack should be retired. For an orphaned PR from
  `list`, add `--pull-request <pr>`; to preview and retire every orphan, use
  `unstack --cleanup --pull-request orphans --dry-run`, then
  `unstack --cleanup --pull-request orphans`.
- **Collect closed or merged leftovers:** `cleanup --dry-run`, then `cleanup`.
  It checks each exact saved PR and removes only verified artifacts for
  closed or merged reviews. Open reviews and open orphans are preserved;
  mismatched or unavailable GitHub state blocks that record.
- **Stop tracking locally but leave PRs open:** `unstack --local`.
- **Change PR base/head because the stack shape changed:** reshape with `jj`,
  then `submit --dry-run` and `submit`.
- **Recover after GitHub merges:** `sync --dry-run <head-change-id>`, then
  `sync <head-change-id>` chains the repair — fetch, retire merged ancestors,
  rebase selected survivors, and update their existing PRs. GitHub rebase
  merges preserve jj change IDs; squash merges do not, and `sync` handles both
  from the fetched merge result.
- **Adopt existing PRs into local tracking:**
  `checkout --pull-request <pr> --fetch` for a whole stack (fetches the reviewed
  commits and saves tracking without moving the working copy, rewriting
  existing changes, or touching GitHub), or
  `relink <pr> <revset>` for one PR/change link.
- **Fresh PRs for the same local changes:** retire the old review first with
  `unstack --cleanup --dry-run <revset>` then `unstack --cleanup <revset>`,
  and then `submit <revset>`. There is no restart flag; submitting without
  retiring the old review reuses the existing PRs.

If a direct GitHub mutation already happened, do not rebuild changes or PRs
by hand. Inspect with `list --json`, `view --pull-request <pr> --json`, and
`doctor`, then choose `checkout`, `relink`, `submit`, or `unstack` from what
you see.

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
5. When bottom changes are ready, `merge --dry-run`, then `merge`, followed
   by the printed `sync <head-change-id>`.
6. If `trunk()` merely advanced, use plain `jj rebase`. `sync` is for
   ancestors already merged on GitHub under exact or rewritten commit IDs.

## Exit codes

0 success; 1 any other failure, including a blocked action; 2 selection is
not a supported stack; 3 unresolved conflicts; 4 GitHub auth/API failure;
5 invalid arguments; 6 ambiguous selector (fails closed — use `relink` to
repair an incorrect attachment or select explicitly); 10 `view`/`list` printed a report
that is incomplete or needs attention (the output is still valid — read it);
130 interrupted.

## When something goes wrong

- `merge` rejected by GitHub: read the reported check, conflict, queue,
  policy, or access reason, fix it, and rerun the same explicit
  command. A native terminal failure merges nothing. If ordinary bottom-up
  merging accepted lower PRs first, run `sync <head-change-id>` before retrying the
  remainder. A matching request already pending should be allowed to finish,
  then observed by rerunning the same target and method.
- Interrupted command: `view`, then rerun with an explicit change ID, revset,
  or `--pull-request` selector.
- jj-stack reports ambiguity (exit 6): stop and ask for a concrete selector.
- Stale workspace: `jj workspace update-stale`.
- Local recovery: `jj op log` and `jj undo`; never destructive git commands.
- Imported review bookmark or interrupted checkout/sync leftovers: run `doctor`
  and follow the exact recovery command it prints.
- Auth or remote resolution unclear: `doctor`.
