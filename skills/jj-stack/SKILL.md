---
name: jj-stack
license: Apache-2.0
description: >
  Manage jj-native stacked GitHub review with jj-stack. Use when inspecting,
  creating, submitting, refreshing, revising, landing, cleaning up, or
  recovering stacked pull requests for local jj changes, and before mutating
  any GitHub pull request or branch with gh or the GitHub API in a jj repo.
---

# jj-stack

`jj-stack` sends a linear chain of local `jj` changes to GitHub as dependent
pull requests. Division of labor: `jj` edits the local stack; `jj-stack` owns
its GitHub review state (review branches, PRs, landing, cleanup).

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
   `git branch`/`checkout`/`rebase`, manual branch pushes, or `gh stack` on a
   jj-stack stack, and never create, delete, or force-push its review
   branches by hand.
2. **Check ownership before the first `gh` or API write in a repo.** Run
   `jj-stack list --json`, or `jj-stack view --pull-request <pr> --json
   --fetch` for one PR. If the PR, branch, bookmark, or change appears in the
   output, the stack is managed; cache that answer for the session. Do this
   lazily — the trigger is a pending GitHub write, not entering a repo. These
   commands exit 10 when they print a report that is incomplete or needs
   attention; read the JSON before concluding anything.
3. **Use jj-stack as the stack authority.** Once jj-stack is detected anywhere
   in a repo, use it for stack-level PR work in that repo: status, submit,
   refresh, base/head changes caused by stack rewrites, landing, cleanup,
   closing, importing, relinking, and recovery. `gh` remains fine for reads and
   collaboration metadata, but not as the source of truth or mutation path for
   the stack.
4. **Inspect before mutating.** Run `view` or `list` before `submit`, `land`,
   `cleanup`, or `unstack`, and preview with `--dry-run` whenever the next
   step is uncertain.
5. **Select explicitly after anything ambiguous.** `submit` defaults to the
   current stack head (`@-`). After an interrupted command, or in a
   multi-stack repo, pass a change ID, revset, or `--pull-request` selector.
   Prefer change IDs in user-facing summaries; use commit IDs only when a
   concrete immutable snapshot matters.
6. **Stay non-interactive.** Do not pass an interactive `--describe-with`
   helper; instead pass a description using `--describe`.

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
local changes, review bookmarks, and tracking data. Map the intent to a
jj-stack command instead; use `gh` only if the user explicitly confirms after
you explain that risk.

- **Land ready bottom changes:** `land --dry-run`, then `land`. Lands the
  consecutive ready changes at the bottom and stops before the first unready
  one; `--pull-request <pr>` stops earlier.
- **Close an abandoned stack's PRs:** `unstack --dry-run`, then `unstack`.
- **Also remove review branches and tracking:** `unstack --cleanup`, only
  after confirming the stack should be retired. For an orphaned PR from
  `list`, add `--pull-request <pr>`.
- **Stop tracking locally but leave PRs open:** `unstack --local`.
- **Change PR base/head because the stack shape changed:** reshape with `jj`,
  then `submit --dry-run` and `submit`.
- **Recover from a squash/rebase merge made on GitHub:**
  `cleanup --rebase --dry-run`, `cleanup --rebase`, then `submit` to refresh
  surviving PRs.
- **Adopt existing PRs into local tracking:**
  `checkout --pull-request <pr> --fetch` for a whole stack (sets up tracking
  only; rewrites nothing and does not touch GitHub), or
  `relink <pr> <revset>` for one PR/change link.
- **Fresh PRs for the same local changes:** `restart --dry-run <revset>`,
  then `restart <revset>` and `submit <revset>`.

If a direct GitHub mutation already happened, do not rebuild changes or PRs
by hand. Inspect with `list --fetch --json`, `view --pull-request <pr> --json
--fetch`, and `doctor`, then choose `checkout`, `relink`, `unlink`,
`restart`, or `unstack` from what you see.

## Everyday flow

1. Build or revise the stack with `jj`. Each change is one reviewable PR:
   put a dependency in the same change or a lower one, and unrelated work in
   a separate stack.
2. Confirm the shape with `view` (`--json` for machine-readable output,
   `--fetch` when current remote branch positions matter); `list` shows the
   repo-wide inventory.
3. `submit --dry-run`, then `submit` to create or refresh PRs. Add
   `--re-request` only when the user wants previous reviewers asked again.
4. Apply review feedback in the change it belongs to: edit the lower `jj`
   change, let descendants rebase, then `view` and `submit`. Do not patch a
   higher change to avoid touching a lower one.
5. When bottom changes are ready, `land --dry-run`, then `land`.
6. If `trunk()` merely advanced, use plain `jj rebase`. `cleanup --rebase` is
   only for ancestors already merged on GitHub under different commit IDs.

## Exit codes

0 success; 1 any other failure, including a blocked action; 2 selection is
not a supported stack; 3 unresolved conflicts; 4 GitHub auth/API failure;
5 invalid arguments; 6 ambiguous selector (fails closed — repair with
`unlink`/`relink` or select explicitly); 10 `view`/`list` printed a report
that is incomplete or needs attention (the output is still valid — read it);
130 interrupted.

## When something goes wrong

- Interrupted command: `view`, then rerun with an explicit change ID, revset,
  or `--pull-request` selector.
- jj-stack reports ambiguity (exit 6): stop and ask for a concrete selector.
- Stale workspace: `jj workspace update-stale`.
- Local recovery: `jj op log` and `jj undo`; never destructive git commands.
- Auth or remote resolution unclear: `doctor`.
