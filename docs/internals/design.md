# jj-native stacked GitHub review: design

## Summary

`jj-stack` turns a linear chain of `jj` changes into a stack of GitHub pull requests
without making side metadata the source of truth.

The model is small:

- one reviewable unit is one visible mutable `jj` change, identified by its full `change_id`
- one stack is a linear chain of those changes from a chosen head back to `trunk()`
- each change gets one bookmark, used as that change's PR head branch
- the local stack is rediscovered from the `jj` DAG on every run, not from a saved parent map

The only thing `jj-stack` saves locally is a small per-change record holding the bookmark
name, the PR number/URL, and a couple of flags. Everything else is derived. That keeps the
tool feeling like an extension of `jj` rather than a parallel stack manager.

## Recommended GitHub policy

The workflow assumes a few GitHub settings, because some branch-history shapes do not map
cleanly back to a single local stack:

- `main` requires linear history
- the configured review-branch prefix (default `review/*`) requires linear history
- the repo allows squash and/or rebase merges so linear history stays mergeable
- a required check or workflow blocks PRs whose base branch matches the review-branch
  prefix from being merged on GitHub

The last rule is important. Linear-history protection alone is not enough: GitHub will
still happily squash- or rebase-merge a PR whose base is `review/*`, and the resulting
branch state is hard to map back to the local `jj` stack.

The intended policy is:

- PRs targeting `main` may be merged
- PRs targeting `review/*` are review-only and must not be merged directly on GitHub

When `jj-stack` sees a merged PR whose base matches the review-branch prefix, it diagnoses
that as a repo policy problem and tells the user to fix the GitHub setting. It is not a
mysterious stack failure.

## Design goals

1. Make stacked GitHub PRs feel native in a `jj` workflow.
2. Be easy to use.
3. Avoid out-of-band metadata as a source of truth.
4. Keep branch names stable across rewrite-heavy review.
5. Recompute as much as possible from `jj` state on every run.
6. Keep any persisted state optional, minimal, and tool-owned.

## Relevant `jj` constraints

A few `jj` properties drive this design:

- There is no "current bookmark". Bookmarks do not move when you create a new commit, but
  they do follow rewrites of the commit they are attached to.
- A bookmark is the remote-branch boundary: a local bookmark is what gets pushed as a Git
  branch.
- GitHub review is still branch-based. Even in a `jj` workflow, GitHub wants a head branch
  and a base branch per PR.
- `jj git push --change` can generate stable bookmark names from `change_id`.
- `jj` already tracks remote bookmark positions and does the safety checks needed for
  force-push-heavy workflows.
- `change_id` is the durable logical identity of a change across rewrites. The commit ID
  is not.
- Both `jj-lib` and the CLI are moving integration surfaces; this tool keeps its
  assumptions narrow.
- `jj`'s internal storage is not an extension API; the tool does not write into `.jj/`
  internals.

## Mental model

### Review change

A review change is one visible mutable `jj` change, identified by full `change_id`. That
is the durable identity — not the commit ID, not the bookmark name, not the current diff
base.

"Visible mutable" follows `jj`'s own revsets:

- visible: the commit is in `visible()` (not a hidden predecessor)
- mutable: the commit is in `mutable()`, with immutability defined by the repo's
  `immutable_heads()`

By default that means `trunk()`, tags, and untracked remote bookmarks define immutable
history. If the repo customizes `immutable_heads()`, `jj-stack` honors that rather than
maintaining its own competing notion of what is safe to review or rewrite.

### Review stack

A stack is a linear chain of review changes from a chosen head back to `trunk()`.

Commands that operate on a stack validate only that one parent chain. Other visible
children elsewhere in the DAG are separate stacks, not an automatic error.

`jj-stack` only supports linear stacks. It rejects (or asks for manual help with):

- merge commits inside the chain
- divergent changes
- multiple reviewable parents
- a path that branches into a tree instead of staying a simple chain

If an ancestor on the chain has other reviewable children, those are separate PR chains
and out of scope for the current command unless the command explicitly asks about more
than one stack.

`jj` can model all those shapes; GitHub's stacked-PR UX gets much harder once the unit
is no longer a simple parent-child chain.

### Pull request branch

Each review change gets exactly one bookmark, used as the GitHub PR head branch. The
bookmark name is readable to humans and stable for tooling.

By default it is built from:

- the configured prefix from `[jj-stack] bookmark_prefix` (default `review`)
- a slug from the first line of the commit description
- a short fixed-length `change_id` suffix (8 chars by default)

```text
review/<slug-from-subject>-<change_id.short(8)>
```

Example:

```text
review/fix-bookmark-resolution-ypvmkkuo
```

The slug helps reviewers using the GitHub UI or plain Git. The `change_id` suffix keeps
the name tied to the logical change without becoming noisy. Eight characters is stable,
readable, and effectively unique once combined with the slug. If a collision is ever
detected, the tool can extend the suffix or fall back to the saved name.

The slug is only an input to the *initial* default name. Once a bookmark is created it is
not automatically renamed when the commit subject changes — title churn must not cause
branch churn during review.

In other words: generate once, then pin. The resolution order is:

1. an explicit override, if present
2. a name already known from local state, the tracking state file, or an existing PR for
   this change
3. otherwise, a matching existing bookmark selected through the configured `use_bookmarks`
   patterns
4. otherwise, generate the default from the current subject and `change_id`, then save
   that choice

If two changes resolve to the same bookmark, `submit` stops before mutating anything.

Bookmarks matched through `use_bookmarks` are external names rather than tool-managed
review bookmarks. `submit` may push them, but later cleanup does not delete or forget
them by default.

### Review base

The GitHub base branch for a review change is:

- the parent review change's bookmark, if the parent is also being reviewed
- otherwise the trunk branch

This is where GitHub still imposes a branch model on top of `jj`. `trunk()` defines the
stack boundary in commit space, but it does not by itself give GitHub a base-branch name.
For GitHub operations the tool has to resolve trunk to one concrete remote bookmark on
the chosen remote, e.g. `main@origin`.

The trunk base must be one of:

- the chosen remote's default branch as reported by GitHub
- or an unambiguous remote bookmark on that remote whose target is `trunk()`

If `trunk()` falls back to `root()` or cannot be mapped to exactly one remote bookmark on
the target remote, `submit` errors out rather than guessing.

### Workspaces

Tracking state is shared across workspaces for the same repo (see Storage strategy).
Repo-scoped discovery treats every workspace's working-copy commit as workspace state,
not as an extra review change: `list` excludes those commits regardless of which workspace
invoked it. Explicit selection may use a non-empty working-copy commit, but an empty
working-copy commit from any workspace is not reviewable. Only the invoking workspace's
working-copy commit is marked as current in user-facing output.
Stale working copies are a local workspace concern, not a separate review concept: if
`jj` reports a stale workspace, the tool stops and points the user at
`jj workspace update-stale`. Divergence caused by concurrent rewrites from multiple
workspaces is unsupported and errors out.

## What is derived vs. stored

### Derived from `jj` every time

These need no tool-owned state:

- stack topology
- parent-child relationships
- diff base inside the stack
- current head commit for a change
- whether a bookmark needs to move after a rewrite
- whether a bookmark is ahead of its tracked remote

All of that already lives in the commit DAG, the change-ID model, and the bookmark view.

### Stored in the tracking-state file

If anything is saved locally, it is a small record per change:

- the pinned bookmark name (once chosen)
- PR number and URL
- the navigation/overview comment IDs, if used
- the last known PR state and review decision, used as a fallback when GitHub is offline
- the last submitted `commit_id`, compared against the live commit to detect rewrites
  that keep the same stack position
- topology pointers `last_submitted_parent_change_id` (or null for trunk) and
  `last_submitted_stack_head_change_id`. These are compared per change against the
  current DAG to detect when a tracked chain has changed since the last successful
  `submit` — never aggregated into a stack-level comparison
- a durable "unlinked" marker for a change the user explicitly detached, because that is
  user intent the tool must not silently undo

A change can be in one of three link states:

- **untracked**: no record yet. Predicted bookmark names and remote observations alone do
  not count as tracking.
- **tracked (active)**: a record exists; the tool inspects and updates it normally.
- **unlinked**: a record exists, but the user explicitly detached it. The tool must not
  silently reattach.

Even the PR link can usually be rediscovered by asking GitHub for the PR whose head
branch matches the saved bookmark, but that rediscovery is an explicit recovery flow —
plain `view` does not do it for never-tracked changes.

During a direct-push `land`, the same file temporarily holds one repo-level pending
transaction. It records exact operation scope and finalization progress, not stack topology,
and disappears atomically with the landed per-change records when finalization completes.
Other commands preserve it when updating their own tracking fields; a later `land` either
finishes it exactly or fails closed if those commands changed its review identities.

User-authored settings (e.g. reviewer or label preferences, `use_bookmarks` patterns)
live in `jj` config, not in the tracking-state file.

The tool also writes a stack-summary comment onto each PR (the navigation/overview
comments described in the submission algorithm). That summary is not a source of truth —
it is regenerated on every `submit` from the current `jj` stack. `submit`, `unstack`,
and `cleanup` may read comments to re-find or delete comments the tool previously wrote,
but `view` does not inspect issue comments.

## Storage strategy

Do not write into `jj` internals (`.jj/repo/store/extra/`, the view/op store, private
ref namespaces). Those are tempting but tie the tool to storage details `jj` keeps
flexible.

Do not store config or tracking state in the working tree. Tracked workspace files are
the wrong default for both:

- config in the working tree looks like project-shared policy and is too easy to commit
- tracking state in the working tree dirties the `jj` working copy and perturbs the
  history the tool is supposed to map to GitHub

So storage splits in two:

- human-authored config in `jj`'s normal config scopes under the `jj-stack` namespace
- tracking state in `~/.local/state/jj-stack/repos/<repo-id>/state.json`

Repo defaults follow `jj`'s own precedence:

- user config (`jj config edit --user`)
- repo config (`jj config edit --repo`)
- workspace config (`jj config edit --workspace`)

That keeps `jj-stack` aligned with `jj`'s config model rather than inventing a parallel
conditional-matching system.

`<repo-id>` is derived from the canonical `.jj/repo` storage path. In the primary workspace,
`.jj/repo` is that storage directory. In an additional workspace, `.jj/repo` is a path file
pointing at the same directory; resolve its contents relative to the workspace's `.jj`
directory before canonicalizing and hashing it. The path file itself is never a repository
identity. That keeps state repo-scoped across workspaces without a separate bootstrap step and
without writing any tool-specific file into the workspace.

Reads treat a missing state file as empty state. Writes create the parent directory on
demand and only fail if the filesystem refuses.

Mutating commands take a repo-scoped advisory operation lock in that same state
directory before reading or writing state. The lock serializes cross-command mutation; its
companion file records the owning command, PID, and start time for diagnostics. `list` and
`doctor` do not take the lock.
`view` does not lock for live inspection, but it tries the lock around its best-effort
cache write and skips that write with a diagnostic if another operation is running.

Mutating commands append operation events to the repo-level `operation-log.jsonl` audit log.
The log is not a topology source of truth or a recovery database; it is evidence for
explaining what happened after the fact. Direct-push `land` instead stores one typed pending
transaction beside the per-change tracking state. That checkpoint exists only while a trunk
transition may need recovery and is cleared atomically with the landed tracking after
finalization.

## Submission algorithm

Given a chosen head revision:

1. Resolve the head. When the user runs `submit` with no `<revset>`, the head is `@-`.
   `@` stays explicit user intent and is never selected by an omitted argument. Print the
   selected head after the transient preparation status has cleared, so persistent output
   starts on its own line.
2. Walk the parent chain down to the stack boundary, building a linear chain of visible
   mutable changes. The boundary is usually `trunk()`, but may also be a recent shared
   ancestor of the head and `trunk()`, or an allowed merged-side boundary discovered
   during status-style inspection.
3. Reject ambiguous shapes rather than papering over them with metadata. Also stop if any
   change in the stack still has unresolved conflicts: `submit` must not push a
   conflicted snapshot.
4. Resolve each change's bookmark using the reuse-first order from "Pull request
   branch" above.
5. For saved review branches, verify the selected remote's actual branch head before
   asking GitHub for PR state. The remote head is safe only when it still points at the
   saved submitted commit, or already points at the selected change's current commit
   after a previous failed submit pushed the branch. Any other target means the
   branch drifted outside this submit, so `submit` stops before local bookmark, remote
   branch, or GitHub mutation.
6. Look up GitHub PR state for those bookmarks.
   - if the saved PR link disagrees with what GitHub reports, stop and require an
     explicit recovery flow rather than silently creating a replacement PR. This
     check covers every change in the selected stack and completes before any local
     bookmark move, remote branch push, or GitHub mutation, so a mid-stack link
     failure cannot leave sibling changes half-submitted.
   - by default, the PR title comes from the commit subject and the PR body from the
     remaining commit description; if there is no body, fall back to the repository's
     pull request template, and finally to the subject so the opening comment is not
     blank
   - the pull request template is the first existing file among
     `.github/PULL_REQUEST_TEMPLATE.md`, `PULL_REQUEST_TEMPLATE.md`, and
     `docs/PULL_REQUEST_TEMPLATE.md` (upper- or lower-case filename) under the workspace
     root. An empty template counts as absent. Because the PR body is re-derived on
     every submit, the template applies to updates the same way it applies to creation;
     it never overrides a change description body or an explicit `--describe` /
     `--describe-with` result.
   - `submit --describe <change>=<file>` replaces one PR body with Markdown read from
     `<file>`, while keeping the PR title from the change subject. The `<change>` selector
     must resolve to exactly one change in the selected stack.
   - `submit --describe stack=<file>` uses Markdown read from `<file>` as the head PR's
     stack overview comment for a multi-change stack.
   - `--describe` may be repeated. Relative file paths are resolved from the current
     directory where `jj-stack` was invoked, not from the selected repository.
   - `submit --describe-with <helper>` replaces that default by invoking the helper once
     per change (`helper --pr <change_id>`), and once per stack
     (`helper --stack <revset>`) for stack-level prose
   - the per-stack invocation only fires when the stack contains more than one change;
     its output becomes a stack overview comment on the head PR. It is not used as a
     topology source.
   - for stacks with more than one change, every PR also gets a single navigation
     comment listing every PR top-to-bottom with a trunk line beneath the bottom-most
     PR. The current PR's title is bold and marked "this PR"; the other titles link.
   - when `helper --stack` returns non-empty content, the head PR also gets a single
     overview comment containing the helper-generated stack prose.
   - if a PR on the selected stack previously held an overview comment but is no
     longer the stack's head (because the head moved or the stack shrank), that
     overview comment is deleted as part of regeneration. The single-change case
     below is a specialization of the same rule.
   - for stack helpers, `submit` writes a temporary input file with the per-PR title/body
     pairs and a compact diffstat for each PR, and points the helper at it via
     `JJ_STACK_INPUT_FILE`. Helpers can summarize from PR-level metadata rather
     than replaying the full patch history.
   - helper output must be structured. Invalid output aborts `submit` before any local,
     remote, or GitHub mutation.
   - `submit --edit` opens the user's editor once with the planned title and body of
     every PR in the selected stack, pre-filled from the defaults above (including any
     `--describe` files), rendered top-to-bottom like `view`. The edited document
     replaces those titles and bodies. Invalid edits — content before the first change
     separator, an unknown, repeated, or missing change section, or a section with no
     title line — abort `submit` before any local, remote, or GitHub mutation, as does
     a non-zero editor exit. The editor is the one jj's `ui.editor` resolves to
     (including its `$VISUAL`/`$EDITOR` fallbacks); `--edit` cannot be combined with
     `--describe-with`, whose helper already owns description authoring.
7. Treat merged ancestors as no longer reviewable. Bottom-up for each remaining change:
   - point the local bookmark at the current visible commit for the change
   - treat topology changes as meaningful even when the diff is unchanged: if the parent
     review change, bookmark target, or PR base changed, this is not a no-op
   - if the chosen remote bookmark already points at the desired commit, treat it as up
     to date even when the local repo has not yet tracked that remote
   - if the local bookmark or remote bookmark is conflicted, stop and ask the user to
     resolve the bookmark state first
   - if the remote bookmark exists but points elsewhere, only proceed when the match is
     already proven by local state, the saved record, or GitHub discovery; otherwise
     stop rather than silently taking over the branch
   - when updating exactly one existing untracked remote bookmark, do not import its old
     target into the local bookmark before the remote update completes
   - if a submit would update multiple remote bookmarks and any update needs the
     untracked-remote fallback, stop before moving local bookmarks or mutating GitHub;
     the user must fetch and track those branches first so the stack can be pushed as one
     atomic update
   - before pushing rewritten review branches, protect existing open PRs from GitHub's
     reachability-based close/merge behavior: for every open PR whose head ref is in
     the planned push set, simulate the post-push commit IDs of head and base; if the
     post-push head is reachable from the post-push base, first retarget that PR to
     the resolved trunk branch so the push lands without GitHub seeing a head fully
     contained in its base. The simulator resolves a base ref through the push set
     first, then through the local view of the push remote's tracked target. When
     neither is available — for example, a PR whose base lives on a remote that has
     not been imported into the push remote's tracking — the predictor skips that PR
     rather than guess; the post-submit closure check below is the catch-all for
     anything the predictor cannot model.
   - otherwise push the bookmark
   - compute the GitHub base branch:
     - the nearest still-open ancestor PR in the chain, if any
     - otherwise the resolved trunk branch
   - if an ancestor PR has merged but the local parentage still reflects the old review
     stack, require a local `jj rebase` before changing the PR base
   - create or update the PR for `head bookmark → base bookmark`
   - once `submit` finishes, render the stack top-to-bottom through the same native
     `jj log` row formatting that `view` uses, with concise submit-result text
     appended to the first line of each row, and the resolved trunk row beneath
   - draft handling stays conservative:
     - `submit --draft` / `submit --draft=new` opens new PRs as drafts
     - `submit --draft=all` also returns existing published PRs to draft
     - `submit --open` marks existing draft PRs ready for review and creates new PRs
       as published
     - plain `submit` preserves the draft state of already-open PRs
     - plain `submit --draft` does not turn a published PR back into a draft
   - `submit --re-request` asks GitHub to request review again from users whose latest
     review on the PR is `APPROVED` or `CHANGES_REQUESTED`. It does not disturb
     still-pending review requests.

The bottom-up ordering matches stack dependency order, and the parent relation is read
from the DAG, not from saved metadata.

Submission is allowed to have brief intermediate GitHub states, but they must preserve
review identity. In particular, a rewritten stack must not leave an existing selected-stack
PR pointing at a base branch that now contains that PR's head; GitHub can interpret that as
merged and close the PR before `submit` finishes repairing the stack. If a submit is
interrupted after the protective trunk retarget and before final PR sync, the result should
be a repairable flat or partially restacked set of the same open PRs, not closed or replaced
reviews.

After all PR mutations and stack-comment work succeed, `submit` refetches the GitHub
state of every PR that was open when the run began and fails the command if any of them
are no longer open by the end. `submit` itself never closes or removes a PR on purpose,
so an open→closed transition is unambiguous evidence that GitHub's reachability-based
auto-close fired in a way the pre-push predictor did not anticipate, and an open→missing
transition means the PR was deleted or transferred during the run. The check is
detection, not repair: it turns silent data loss into a loud error naming the affected
PRs so the operator can reopen or restore them on GitHub. Defense-in-depth for the
predictor, not a substitute.

For a stack with exactly one change, `submit` behaves like a plain PR-submit flow: no
stack helper invocation, no navigation or overview comments, and any older nav/overview
comments left from a previous larger stack are deleted. After a successful live submit,
the URL of the top of the stack is printed so the user can open it in a browser.

There is no meaningful stack metadata to add when the stack has only one PR.

## Recovery and repair

When review identity is unclear, `jj-stack` is conservative.

If `submit` cannot prove that a change still corresponds to the same review branch and
PR, it stops with a targeted diagnostic rather than guessing. It does not silently open
a new PR just because a saved link, bookmark, or GitHub state is missing or damaged.

The recovery surface is explicit and narrow:

- `jj stack view --fetch [<revset>]` refreshes remembered remote-branch observations
  before inspecting GitHub PR state, then reports the stack and any saved or discovered
  PR state without mutating GitHub or local bookmarks
- `jj stack relink <pr> <revset>` is a repair command. It explicitly reattaches an
  existing PR (and its same-repo head branch) to a specific `jj` change. It pins the
  branch locally and saves the PR identity so a later `submit` updates the relinked
  review rather than opening a replacement.
- `jj stack restart <revset>` is a repair command for abandoning stale or unusable
  PR tracking on a selected stack. It keeps the `jj` changes, clears their previous PR
  identity, assigns fresh managed review bookmark names, and leaves the next `submit`
  to create replacement PRs explicitly.
- `jj stack submit --restart <revset>` is the user-facing one-step version of that
  repair. It computes the same fresh tracking state in memory, creates replacement
  PRs, and only persists the new PR identity as part of the successful submit path.

Selector defaults are listed once under "CLI shape" below. The principle: lifecycle
commands default to the stack headed by `@-`; repair commands (`restart`, `relink`,
`unlink`) require an explicit `<revset>`; `@` is always explicit user intent and is
never selected by an omitted argument.

### `view`

`jj stack view [<revset> ...] [--pull-request <pr> ...]` shows the local stack(s) and
any locally known review identity for them.

It is local-first. If a change has never been locally attached to review, `view`
reports it as not submitted and does not query GitHub for speculative PR matches based
only on predicted bookmark names or fetched remote observations. It does not create
local tracking for a never-tracked change, including bookmark-only saved entries.

`jj stack view --fetch [<revset> ...] [--pull-request <pr> ...]` is the same command,
but it refreshes remote bookmark observations first so the report reflects the latest
remote state before checking already-known GitHub PR state.

When more than one selector is given, `view` inspects them in command-line order,
suppresses exact duplicate stack reports, continues past selector-local resolution
failures, and exits with the incomplete-report code if any individual stack would have
done so. A single selector behaves like bare `view`: a failure that prevents any report
propagates with its category code (for example, unsupported stack shape exits `2`)
instead of degrading to the incomplete-report code, so the exit code for a drifted state
does not depend on whether the selection was explicit.

Fetched GitHub state often produces extra visible revisions for merged changes, so
`view` does not insist that every visible revision still forms one supported review
stack. It walks the parent chain, tolerates immutable or divergent side copies created
by fetching merged PR branches, and reports the stack revision for each logical change.
If a merged PR still appears on the stack, `view` continues and surfaces that row as
"cleanup needed" rather than calling the stack broken. If the local history no longer
has any supported linear walk after refresh, `view` stops with a targeted diagnostic
rather than a traceback or an unadorned subprocess error.

Unlike `submit`, `view` may fall back to local-only output when the repo is not
configured well enough to resolve a remote or GitHub target. Default output stays
concise — one effective summary per change rather than dumping saved-data and transport
diagnostics inline.

With `--json`, `view` prints a structured version of that same per-change summary. The
payload includes the selected stacks, their changes, review bookmark names, PR identity,
and concise review status. It does not expose cache state, raw remote bookmark targets,
or saved tracking records; command failures and incomplete inspection still use stderr
and the process exit status. The machine-readable schema for the public output lives in
[`docs/json-output.schema.json`](../json-output.schema.json).

`view` may add a repo-level advisory for other tracked stacks when the saved
submitted state disagrees with the current DAG: either a tracked change's saved
`last_submitted_commit_id` differs from its current commit, or the saved topology
pointers (`last_submitted_parent_change_id`, `last_submitted_stack_head_change_id`)
no longer match the live chain. The advisory names the stack heads and points the
user at running `view` on each, because the correct follow-up depends on the cause.
Stale comments alone do not trigger the advisory.

The stack revisions and the footer row beneath them both render through the user's
native `jj log` formatting; status-specific suffixes (PR state, etc.) are appended to
the first rendered line. The footer row shows the stack's `base_parent` (the immediate
parent of the bottom change), which may or may not be the resolved `trunk()`.

When GitHub data is available, `view`:

- distinguishes merged PRs from merely closed ones
- surfaces a concise review-decision summary (approval, changes requested) for open PRs
- renders open draft PRs differently from open published PRs
- if GitHub is unreachable or misconfigured, reports that once at the repo level and
  falls back to conservative per-change summaries from tracking data rather than claiming a
  PR is absent. Because the output is incomplete, `view` exits with the incomplete-report
  code
- if it finds an ambiguous PR match, surfaces that inline and exits with the
  incomplete-report code rather than silently calling the stack healthy
- if a saved PR link existed but GitHub reports no PR for that branch, looks up the
  saved PR number before rendering the result; if GitHub still cannot find a PR, it
  surfaces the stale link inline and exits with the incomplete-report code without
  clearing the saved PR identity
- when the link is stale, closed, or ambiguous, prints a short repair advisory that
  distinguishes reopening the same PR, relinking an open replacement, and running
  `submit --restart` to create fresh PRs
- when a saved PR link includes a last-known PR state, surfaces that as tracking data
  rather than implying it is live
- does not inspect managed stack-summary comments. Those comments are derived review
  artifacts, and the commands that create or delete them own their validation.
- on a successful live run, refreshes the saved link bidirectionally when GitHub
  reports a concrete PR; missing branch lookups preserve saved PR identity as recovery
  evidence

When `view` reports `cleanup needed`, it explains why in plain language:

- a merged PR still appears on the local stack
- descendant `submit` operations will keep following that old ancestry until the user
  repairs it
- the next command is `jj stack cleanup --rebase [<revset>]`; add `--dry-run` first to
  inspect the rebase plan before mutating local history

That guidance matters more than the internal distinction between "selected path",
fetched branch-tip artifacts, and off-path immutable copies. The tool still needs those
concepts internally, but the user sees actionable wording, not a terse label.

### `list`

`jj stack list [--fetch]` gives one repo-scoped summary row per locally known stack. It
is local-first too: discover stacks from saved tracking plus any visible local
descendants above those tracked changes; do not create tracking for remote-only state;
do not speculate about GitHub-only stacks that have never been attached locally.

The summary row identifies the stack by head `change_id`, shows stack size, gives a
compact PR summary, and highlights unusual local states such as divergence, conflicts,
or merged PRs needing cleanup. The text table shows the exact PR number for a single-PR
stack and summarizes multi-PR stacks by count so long stacks do not crowd out the
description column. If GitHub is unavailable or a saved PR link has gone stale, the row
surfaces that and `list` exits with the incomplete-report code rather than reporting a
healthy tracked stack from incomplete data.

Like `view`, `list` may surface tracked stacks whose submitted state no longer
matches the live DAG, naming the heads and pointing the user at `view` for the
per-stack next step.

`list` also surfaces orphaned PRs — saved tracking records whose change is no longer
present in any current stack — as their own rows, separate from the live stacks. Each
row names the PR, and one advisory after the table points at
`unstack --cleanup --pull-request orphans` to retire every orphan shown. The explicit
single-PR closure path remains `--pull-request <pr>`. Without this surfacing, common
workflows (squashing two reviewed changes by emptying one and abandoning it) would leave
PRs open without the user noticing.

With `--json`, `list` prints the same row model as the text table. Stack rows include
their changes so clients can derive stack length, head change, and PR list directly
from the structured changes. Orphaned PRs remain rows with `type: "orphan"` rather than
a separate internal bucket. The same schema file covers both `view --json` and
`list --json`.

These commands are not sources of truth either. They are user-driven ways to reattach
GitHub state to a `jj`-derived stack after damage, cross-machine work, or manual edits
on GitHub.

### `checkout`

`jj stack checkout [--fetch] [--pull-request <pr> | --revset <revset>]` resolves one
exact stack and sets up tracking for it. It does not mutate GitHub.

`checkout` is the explicit recovery and bootstrap path for review state that already
exists remotely. If a stack already has PRs on GitHub but local tracking is missing on
this machine, `checkout` is what you run. Plain `view` does not do this implicitly.

Selector handling stays unambiguous: a bare positional argument does not double as both
revset and PR number, and omitting selector flags defaults to the stack headed by `@-`.

`checkout --pick` is a third, interactive selector: it lists the locally tracked stacks
(current stack first) numbered on standard output, reads one number from standard
input, and then proceeds exactly as if that stack's head had been passed via
`--revset`. The picker offers only stacks that already have local tracking — attaching
a remote-only stack still requires an explicit `--pull-request`. Empty, non-numeric, or
out-of-range input fails closed with a usage error, and no tracked stacks at all is a
targeted error pointing at `--pull-request`. The prompt happens before the operation
lock is taken so an idle picker never blocks other commands.

`checkout` sets up tracking, not workspace motion:

- without `--fetch`, use only commits and PR-backed state already available locally
- resolve from an explicit PR or an explicit local stack
- with `--fetch`, refresh remote bookmark observations and (for an explicit PR) fetch
  only the branches needed for the stack, so a remote-only reviewed stack can still be
  attached locally
- refresh the tracking entry only for that exact stack
- create or refresh local bookmarks only when the target is exact, same-repo, and
  unambiguous
- when `--fetch` pulls in a remote-selected stack, print the fetched tip rather than
  changing the workspace

`checkout` does not:

- rewrite commits
- restack descendants
- check out the fetched stack into the current workspace
- open, close, or mutate PRs
- delete local history

Failure guidance stays specific:

- if the PR head branch is missing locally, point the user at `checkout --fetch`
- if the PR head branch is missing on the remote, cross-repo, or ambiguous, stop and
  explain that the stack cannot be checked out safely
- if multiple PRs match the same head branch, point at `view --fetch` and `relink`
- if any checked-out revision would need a freshly generated bookmark instead of an exact
  discovered name, stop rather than inventing a local match
- if the fetched stack shape is unsupported locally, point at `cleanup --rebase` only
  when the issue is local ancestry rather than remote identity
- if `checkout` defaulted to the current stack and that stack has no matching PR, say so
  rather than silently doing nothing
- if a local bookmark already points elsewhere, stop and explain the conflict rather
  than silently taking it over
- if a stale saved entry disagrees with a freshly fetched link, the fetched link wins
  only when it is exact and unambiguous; otherwise stop and surface the conflicting
  identities rather than partially overwriting

`view --fetch` stays the read-only refresh path; `checkout` is the explicit
materialization path. The stack-scoped `sync` command below composes refresh,
merged-ancestor repair, and resubmission for one selected stack; a repo-scoped variant
that refreshes several stacks at once remains a separate future question.

### `sync`

`jj stack sync [--dry-run] [<revset>]` chains the routine catch-up flow for one
selected stack into a single command:

1. refresh remote state and drop merged ancestors, exactly as
   `cleanup --rebase [<revset>]` would, including its conservative stops
2. if the rebase step is blocked, stop with the rebase diagnostics and exit `1`
3. if every change on the selected stack has already merged, report that there is
   nothing to submit and exit `0`
4. otherwise run the plain `submit` flow on the re-resolved selected stack

`sync` adds no new mutation surface of its own: it is exactly the composition of the
two commands above under one operation lock, and each phase journals as itself. It
accepts no submit flags; runs that need draft handling, descriptions, reviewers, or
restart semantics use `submit` directly.

`sync` only rewrites history to remove merged ancestors. It does not rebase the stack
onto newer trunk commits when nothing in the stack has merged — that stays an explicit
`jj rebase`.

With `--dry-run`, the rebase plan is previewed; the submit preview follows only when no
rebase work is planned, because a submit preview computed before an unapplied rebase
would describe the wrong stack.

### `unstack`

`jj stack unstack [--cleanup] [--dry-run]
[--pull-request <pr|orphans> | <revset>]` ends review for one stack or an explicit set of
orphaned pull requests.

`unstack` is stack-first. It looks at the local stack, finds the open PRs the tool is
already tracking there, and either runs or previews the actions needed to end review.

`--pull-request <pr>` is usually an alternate selector for the local stack — it must
resolve to one linked local change. The one exception is `unstack --cleanup
--pull-request <pr>` for an orphaned PR (one whose local change has been abandoned
or otherwise dropped from every current stack): saved tracking is the only available
identity, so `unstack` acts from the exact saved PR and branch fields and still fails
closed if either is missing or ambiguous. Before deleting a branch, it verifies that
the saved PR still uses the saved branch name on the configured GitHub repository, not
just a same-named branch from another owner. "Ambiguous" includes the case where the
saved branch is now claimed by another tracked change (e.g. via `use_bookmarks`) —
branch deletion in that mode would silently take a branch out from under a live review.

`unstack --cleanup --pull-request orphans` selects every open-PR tracking record that
`list` reports as an orphan when the command begins. The `orphans` selector cannot be
combined with a revset or `--local`. The command processes targets in pull-request-number
order and applies the same PR-head, bookmark-ownership, and duplicate-claim checks used for
one orphan. A blocked target remains open and tracked; other independently verified targets
continue, and the command exits `1` if any target was blocked. A hard failure stops the
batch with prior successful cleanup preserved. `--dry-run` performs the same selection and
verification without closing PRs or deleting review artifacts.

Without `--cleanup`, `unstack`:

- closes the open PRs the tool is already tracking for the stack
- updates tracking so those changes are no longer treated as actively tracked
- skips already-merged or already-closed PRs rather than treating them as new close
  targets
- leaves local bookmarks and remote PR branches in place

With `--local`, `unstack` removes only the saved local tracking records for the selected
stack. It does not close PRs, delete remote branches, delete local bookmarks, or inspect
GitHub. The local `jj` changes remain in place. This mode is for checkouts that should
stop treating the stack as locally tracked while leaving the GitHub review stack alone.
It cannot be combined with `--cleanup`.

With `--cleanup`, `unstack` also performs conservative post-close cleanup for review
artifacts the tool can verify belong to the stack:

- delete remote PR branches on the configured remote, only when verified to belong to
  the stack
- forget local bookmarks, only when verified to belong to the stack
- delete stack-summary comments belonging to the stack
- prune any leftover review tracking, e.g. a saved stack-summary comment ID
- preserve external bookmarks (e.g. ones reused via `use_bookmarks`) unless the user
  opts in to cleaning them up too

That opt-in is `cleanup_user_bookmarks = true` under `[jj-stack]`. The default is
`false`.

The opt-in stays explicit because closing PRs is less destructive than deleting
branches. Preview output makes the difference clear so the user can choose between
"close only" and "close and clean up". If the tool cannot verify exact local and remote
review identity, `--cleanup` refuses the deletion rather than falling back to
branch-name heuristics.

`unstack` is idempotent:

- rerunning `unstack` on an already-closed path succeeds as a no-op (or with a brief
  "nothing to close")
- rerunning `unstack --cleanup` after an earlier `unstack` performs only the remaining safe
  cleanup, not another close

### `unlink`

`jj stack unlink <revset>` is the repair-oriented inverse of `relink`: it intentionally
detaches one change from active PR tracking without touching GitHub.

`unlink` is an advanced repair command, not the normal way to end a review. Its unit of
intent mirrors `relink`: one change, identified from the local DAG.

`unlink` clears the active link fields:

- `pr_number`
- `pr_url`
- `pr_state`
- `pr_review_decision`
- `navigation_comment_id`
- `overview_comment_id`

It then writes a durable unlinked marker for the change. That marker matters because
simply deleting the saved record would otherwise be reversed by later rediscovery.

Unlinked state means:

- `view --fetch` may still report a discovered remote bookmark or PR for the same
  branch, but it labels them as unlinked rather than reactivating tracking
- `checkout` may restore local bookmark state for the change, but keeps the unlinked
  marker; it does not restore active PR tracking
- a preserved local bookmark surfaces as an unlinked bookmark rather than as actively
  tracked
- `submit` refuses to reuse unlinked state automatically, even if a local bookmark or
  a discovered PR would normally count as proof
- `land` rejects unlinked changes as not safely landable
- `relink` is the explicit way back in; it clears the unlinked marker and reestablishes
  active tracking

By default `unlink` is local-only:

- it does not close PRs
- it does not delete PR branches
- it does not delete stack-summary comments
- it does not refresh remote bookmark observations: a fetch imports whatever the
  remote now holds into the local view mid-repair, and saved tracking plus
  remembered observations already decide link state

It may preserve the local bookmark, but once the unlinked marker exists that bookmark
no longer counts as proof that the change is still being tracked. That precedence rule
is part of the product contract, not an implementation detail.

`unlink` is idempotent:

- unlinking an already-unlinked change is a no-op
- unlinking a change that was never linked errors out rather than creating an unlinked
  marker for a never-linked change

Broader cleanup remains with `cleanup`. Unlinked records do not expire just because the
remote PR disappeared, but `cleanup` prunes unlinked markers whose `change_id` no longer
resolves anywhere in visible history.

### `restart`

`jj stack restart <revset>` prepares the selected stack to be submitted as fresh PRs.
It is for cases where the local changes should continue, but the old PR tracking should
not: closed PRs that should not be reopened, deleted PRs, or broken branch/PR links
left by a tool bug or manual GitHub repair.

Most users should reach this behavior through `jj stack submit --restart <revset>`.
The standalone `restart` command remains the local repair primitive when the operator
wants to inspect or stage the tracking reset separately.

`restart` clears active PR identity fields for every selected stack change that has
previous PR tracking:

- `last_submitted_commit_id`
- `last_submitted_parent_change_id`
- `last_submitted_stack_head_change_id`
- `pr_is_draft`
- `pr_number`
- `pr_url`
- `pr_state`
- `pr_review_decision`
- `navigation_comment_id`
- `overview_comment_id`

It does not mark the changes as unlinked. Instead it writes fresh managed review
bookmark names that still end with each change's short change ID, so the next
`submit <revset>` can create replacement PRs without reusing the stale PR branches.
`restart` is local-only: it does not close PRs, reopen PRs, delete branches, push
bookmarks, or create replacement PRs by itself. Use `restart --dry-run <revset>` to
inspect the planned reset first.

`submit --restart` does not save that reset before GitHub work begins. If submit cannot
create or verify the replacement PRs, the old tracking state remains available for
inspection and recovery. It also fails before pushing if a planned replacement branch
already has an open PR on GitHub, rather than accidentally updating that PR.

## Rewrite behavior

This design behaves well under normal `jj` rewrite-heavy workflows:

- **Rebase**: the commit ID changes, the `change_id` stays stable, and the bookmark
  follows the rewrite. Re-running `submit` updates the existing PR.
- **Squash or amend**: same as rebase. If the workflow then abandons a now-empty
  change (the usual way to collapse two reviewed changes into one), Abandon rules
  apply to that change.
- **Reorder or reparent**: the stack is rediscovered from the DAG; PR base branches
  are recalculated.
- **Insert**: a new mutable change appears on the chain. `submit` opens a PR for it
  and any descendants' PR bases recalculate against the new parent.
- **Abandon**: the change leaves every current local stack and descendants reattach
  to its parent. Its PR becomes *orphaned* — surviving stacks never close, reuse, or
  retarget it, and `cleanup` keeps the saved PR and branch identity until the PR is
  closed, merged, or absent. Explicit closure goes through `unstack --cleanup
  --pull-request <pr>`.
- **Split**: new logical review changes get new change IDs and usually become new
  PRs. The original keeps its `change_id` and PR and is updated normally on next
  `submit`. This is a feature, not a bug.
- **Duplicate**: the duplicate has a new `change_id` and is treated as a new
  reviewable change on whatever stack it lands on; the original keeps its PR
  untouched.
- **Ancestor merged on GitHub**: merged ancestors stop acting as review bases.
  Descendants target the nearest still-open ancestor PR, or trunk if none remain.
  `cleanup --rebase` performs that local rewrite.

### Cross-stack rewrites

When a rewrite changes which stack a change belongs to, the established rules still
hold: identity is by `change_id`, each command operates on one selected stack
(defaulting to `@-`), and ambiguous linkage fails closed. Other affected stacks wait
for their own explicit command.

- **Move changes between stacks**: submitting the user's selected resulting stack
  updates that chain's PRs from the current DAG. Moved changes keep their existing
  PRs and recalculate their bases from the new parent chain.
- **Split one stack into two or more**: submitting one resulting stack updates only
  that chain's PRs. Every other resulting stack waits for its own command.
- **Merge two or more stacks into one**: submitting the merged stack updates every
  change on the chain bottom-up, reusing existing PRs by `change_id` and
  recalculating bases. The merged chain ends up with one overview comment on its new
  head and no internal trace of the old stack boundary.

The same applies when one rewrite affects more than two stacks.

Stacks the user has not yet resubmitted may still display old navigation or overview
comments. That is expected — `submit` does not chase comments on stacks it isn't
operating on, and `land` does not block on stale state outside the selected stack.
`view` and `list` surface those stacks via the submitted-state rule, naming their
heads and directing the user at `view` for the per-stack next step.
Orphaned PRs left behind by a cross-stack rewrite need an explicit
`unstack --cleanup --pull-request <pr>`.

Records left behind by an interrupted command (`submit`, `unstack`, `cleanup --rebase`)
are diagnostic state, not a replay script for the original selector. A later run of the
same command acts on the *current* stack, while keeping enough of the recorded stack
identity to distinguish "exact continuation" from "stack has been rewritten since".
Older records are retired once a later successful run clearly covers the same changes.
One asymmetric case: `unstack --cleanup` is stronger than plain `unstack`, so a successful
`unstack --cleanup` can retire an older interrupted `unstack`, but a later plain `unstack`
does not silently retire an older interrupted `unstack --cleanup` whose branch or
metadata cleanup may still be outstanding.

This is exactly the kind of rewrite-heavy flow `jj` is good at.

## Why no parent metadata

A branch-first review tool often has to remember both a named parent and an exact
parent revision because the review boundary is otherwise ambiguous after rewrites.

In `jj`, the boundary is already the commit's parent relation. The only place branch
identity still matters is at the GitHub boundary, because GitHub wants:

- one head branch per PR
- one base branch per PR

So the tool needs bookmark-backed PR branches, but it does not need a saved parent
graph.

## CLI shape

The full command surface:

- `jj stack submit [--draft[=new|all] | --open]
  [--reviewers <login[,login...]>] [--team-reviewers <slug[,slug...]>]
  [--describe <change>=<file> | --describe stack=<file> | --describe-with <helper>]
  [--edit] [--re-request] [--restart] [<revset>]`
- `jj stack view [--fetch] [--json] [{--pull-request <pr>} | {<revset>}] ...`
- `jj stack status [--fetch] [--json] [{--pull-request <pr>} | {<revset>}] ...`
- `jj stack st [--fetch] [--json] [{--pull-request <pr>} | {<revset>}] ...`
- `jj stack v [--fetch] [--json] [{--pull-request <pr>} | {<revset>}] ...`
- `jj stack list [--fetch] [--json]`
- `jj stack ls [--fetch] [--json]`
- `jj stack restart [--dry-run] <revset>`
- `jj stack relink <pr> <revset>`
- `jj stack unlink <revset>`
- `jj stack unstack [--local | --cleanup] [--dry-run]
  [--pull-request <pr|orphans> | <revset>]`
- `jj stack delete [--local | --cleanup] [--dry-run] [--pull-request <pr> | <revset>]`
- `jj stack cleanup [--dry-run] [--rebase [<revset>]]`
- `jj stack sync [--dry-run] [<revset>]`
- `jj stack checkout [--fetch] [--pick | --pull-request <pr> | --revset <revset>]`
- `jj stack land [--dry-run] [--via <push|merge>] [--merge-method <merge|squash|rebase>]
  [--pull-request <pr> | <revset>]`
- `jj stack completion <bash|zsh|fish>`

`completion` is auxiliary CLI glue. It prints shell completion scripts. It is not a
review-state command and does not inspect the repo, the tracking-state file, or
GitHub.

`status` is a long alias for `view`; `st` and `v` are short aliases. Run with no
subcommand, the executable behaves the same as `jj stack view` on the current stack.

Top-level help groups commands by intent. `--help` and `help` foreground the core
review lifecycle (`submit`, `view`, `land`, `unstack`) plus support commands
(`cleanup`, `checkout`, `sync`). Repair commands (`restart`, `relink`, `unlink`) and
shell-integration glue (`completion`) stay hidden by default and only appear in
`jj stack help --all`. The `help` command itself is hidden parser glue: `jj stack help`
is the same as
`jj stack --help`, and `jj stack help <command>` is the same as
`jj stack <command> --help`. The default top-level help also keeps advanced global
options (`--repository`, `--config`, `--config-file`, `--debug`, `--time-output`) out
of view until `--all`.

Long command help preserves paragraph breaks so multi-paragraph guidance stays
readable.

Target selection is conservative:

- `submit`, `unstack`, `land`, `sync`, and `cleanup --rebase` default to the stack
  headed by `@-` when `<revset>` is omitted
- `submit --draft[=new|all]` and `submit --open` are mutually exclusive
- `submit --edit` and `submit --describe-with` are mutually exclusive; `--edit` composes
  with `--describe` by pre-filling the editor document from the resolved files
- `submit --reviewers` and `submit --team-reviewers` override configured reviewer
  defaults for the current invocation only. Passing either flag requests those reviewers
  even when the selected pull requests are otherwise unchanged; omitted reviewers are not
  removed
- `submit --re-request` re-requests users whose latest review is `APPROVED` or
  `CHANGES_REQUESTED`; pending review requests stay in place
- `restart`, `relink`, and `unlink` require one explicit `<revset>`
- `checkout` accepts at most one explicit selector flag (`--pick`, `--pull-request`, or
  `--revset`) and otherwise defaults to the current stack headed by `@-`
- `view` may omit `<revset>` and inspects the current stack

### Exit codes

Process exit codes are part of the CLI contract. Where a meaning overlaps with the
`gh stack` CLI extension, the code matches, so scripted callers can treat the two tools
alike; codes 7-9 stay reserved because their `gh stack` meanings (rebase in progress,
lock contention, stacked-PR feature unavailable) have no jj-stack analog.

- `0` — success
- `1` — any other failure, including lifecycle commands that stopped on a blocked action
- `2` — the selection does not form a supported review stack
- `3` — unresolved conflicts in the selected changes block the operation
- `4` — GitHub authentication, network, or API failure
- `5` — invalid command-line arguments
- `6` — a selector matched more than one target and the command failed closed
- `10` — `view` or `list` printed a report that is incomplete or needs attention
- `130` — interrupted

Failure categories ride on the error types: `CliError` subclasses declare their category
code, and a generic `CliError` that wraps a categorized adapter error (for example a
GitHub client failure) inherits the adapter's code. `view` and `list` reserve the error
codes for runs that cannot produce a report at all; a run that prints a degraded report
exits with the incomplete-report code instead. The user-facing table lives in
[docs/exit-codes.md](../exit-codes.md).

Notable absences:

- no standalone `rebase` command — `jj` already handles descendant rewrites better
  than Git
- no `track parent` command — the parent relation comes from the DAG
- no generic metadata-repair command — recovery cases stay explicit and narrow

## Implementation notes

### Drive `jj` via the CLI

For the first implementation, shell out to `jj` rather than linking to `jj-lib`.

Use machine-readable templates instead of parsing human log output. `jj` templates can
emit JSON, and the field names and value types are usually stable even though strict
backward compatibility is not guaranteed.

That suggests commands shaped like:

```text
jj log --no-graph -r <revset> -T 'json({...})'
```

with explicit fields for:

- `change_id`
- `commit_id`
- parent commit IDs
- local bookmarks
- remote bookmarks
- description / subject

### Prefer explicit bookmark control

`jj git push --change` is great for interactive use, but the tool manages bookmark
names explicitly. The tool wants to be able to say:

- this change must use this bookmark name
- this bookmark must now point here
- this PR must be based on that parent bookmark

So the core primitive is "create or move bookmark, then push bookmark", not "blindly
push change with generated name".

### Push ordering and atomicity

Pushes for review-branch bookmarks issued by `submit` go through `jj git push
--remote <name> --bookmark <a> --bookmark <b> ...` as a single invocation. That
maps to one `git push` to GitHub and lands as one atomic ref-update batch from
GitHub's perspective. The pre-push auto-close predictor relies on this: GitHub
re-evaluates each PR exactly once per push, so the predictor's simulation of the
post-push commit IDs only needs to match a single landing point, not a sequence
of intermediate states.

The exception is the rare path where exactly one pre-existing review-branch ref is
present on the remote but untracked locally. There the tool falls back to a single
`git push --force-with-lease ...` against the colocated Git store so it can lock
the update against the expected remote target. If this fallback would be combined
with any other remote bookmark update, `submit` stops before moving local bookmarks
or mutating GitHub. The user must fetch and track the branch first, so the later
submit can use the normal atomic `jj git push --bookmark ...` path.

The invariant is therefore: never split the normal-case push into per-bookmark
operations as an optimization, and never mix the untracked-remote fallback with
other remote bookmark updates.

### GitHub integration

The GitHub adapter can use either:

- direct GraphQL or REST calls
- `gh api` as a thin authenticated transport

If plain `gh` commands that expect a Git repo are used in a non-colocated `jj` repo,
remember that `GIT_DIR` may need to point at `.jj/repo/store/git`.

### GitHub mutation surface

Every GitHub mutation the tool issues is enumerated below together with the
destructive default action GitHub may take in response and the in-tool defense that
prevents it. Any new mutation must be added to this list, and any without a
documented defense must either prove the destructive default does not apply or add
one before merging.

- **Push of a review-branch ref** (`jj git push --bookmark`). When the push lands,
  GitHub re-evaluates each open PR and auto-closes (as merged) any whose head ref
  is now contained in its base ref. A reordered stack can make a stale stacked base
  contain a review-branch head it did not contain before. Defense: before pushing
  rewritten review branches, `submit` simulates the post-push commit IDs of every
  open PR's head and base refs and asks `jj` whether the post-push head is reachable
  from the post-push base; any such PR is pre-retargeted to the resolved trunk
  branch, and the normal post-push PR sync restores the final stacked base. As a
  defense-in-depth backstop for cases the predictor cannot model, `submit` refetches
  PR states at the end of the run and raises a loud error naming any PR that
  transitioned from open to closed or to missing during this submit.

- **Deletion of a remote review branch** (`jj git push --delete`, via
  `delete_remote_bookmarks`). GitHub closes any PR whose head ref points at the
  deleted branch. Defense: branch deletion is invoked only by `cleanup`, `unstack`
  (including the `unstack --cleanup --pull-request <n>` orphan sub-mode), and only
  after the corresponding PR is closed, merged, or absent. Open or orphaned PRs keep
  their branch.

- **`update_pull_request(base=…)`**. Setting a PR base to a branch that already
  contains the PR's head triggers GitHub's merged auto-close. Defense: in `submit`,
  base is set bottom-up to the parent change's bookmark — an ancestor of the head,
  not a descendant — and the head ref has already been pushed to its updated
  content. In `land`, retargeting a landed PR's base to trunk is the intended
  close path; the tool follows the implicit close with an explicit
  `close_pull_request`, so the final state never depends on GitHub's auto-close
  firing.

- **`update_pull_request(title|body)`**. The PATCH always carries `base`, `body`,
  and `title` together, so changing only the title or body still re-asserts the
  current base. When base is unchanged this is a no-op for the destructive default
  above; when base changes the rules in the previous bullet apply.

- **`create_pull_request`**. Creating a PR with a base that already contains the
  head would trigger an immediate merged auto-close. Defense: bottom-up creation
  order means the parent's bookmark always reflects an ancestor of the new PR's
  head before the child PR is created.

- **`close_pull_request`**. Destructive by design. Defense: only invoked by
  `unstack` (including the `unstack --cleanup --pull-request <n>` orphan sub-mode)
  or `land`, each on explicit user instruction or after a successful merge.

- **`merge_pull_request`**. Destructive by design: it permanently merges the PR's
  head into its base branch. Defense: only invoked by `land --via merge` on explicit
  user selection, only for PRs that passed the same readiness checks as the
  direct-push transport, and only after the PR's base has been confirmed or
  retargeted to the resolved trunk branch, so a merge can never land review content
  into another review branch. GitHub's own mergeability check remains the final
  gate: a not-mergeable response stops the landing at that PR.

- **`convert_pull_request_to_draft`**. Repo policy may dismiss approvals on draft
  conversion. Defense: only invoked for an existing open PR when `--draft=all` is
  passed, never as part of default `submit` behavior.

- **`mark_pull_request_ready_for_review`**. Repo policy may trigger required-CI
  runs and other ready-for-review workflows. Defense: only invoked when
  `--open` is passed and the existing PR is currently a draft. New PRs are
  created directly through `create_pull_request(draft=…)` and never round-trip
  through this API.

- **`add_labels`**, **`request_reviewers`**. Additive; no destructive default.

- **`create_issue_comment`**, **`update_issue_comment`**. No destructive default.

- **`delete_issue_comment`**. Deletes the targeted comment. Defense: every call
  site passes a comment id resolved from a cached managed-comment id (or from
  `find_managed_comment` matching the tool's content marker), never an id matched
  by free-text alone. `submit`, `unstack`, `cleanup`, and orphaned
  `unstack --cleanup --pull-request` re-verify the body before deletion or only
  delete via marker-matched discovery. `land`
  trusts the cached navigation- and overview-comment ids without re-verifying the
  body, on the rationale that those ids were written by the same tool during the
  most recent successful submit. `cleanup` additionally limits deletion to managed
  comments that no longer represent a live linked stack.

This list is the bar `submit`, `unstack`, `land`, `cleanup`, and any future command
must clear before introducing a new GitHub call.

### Cleanup semantics

`jj stack cleanup` has a concrete, conservative job:

- prune saved entries for changes that no longer exist or no longer participate in
  any stack, except keep the saved PR and branch identity for an open
  orphaned PR until it is explicitly closed or unlinked
- remove stale stack-summary comments only when they no longer represent a live
  linked stack (e.g. the PR is unlinked, or its head no longer matches the
  expected bookmark)
- optionally delete remote PR branches only once the corresponding PR is closed,
  merged, or absent — not while it is still open but orphaned

An orphan record must include a saved PR number to count as an open orphan; otherwise
there is no concrete PR identity for `unstack --cleanup --pull-request <pr>` to retire.
Cleanup may prune that saved record, but it must not delete the remote branch because
it cannot prove whether an open PR still uses it.

`cleanup` mutates by default; `cleanup --dry-run` shows the planned actions. Deleting
open PRs or deleting branches in ambiguous cases still requires explicit user intent.

`jj stack cleanup --rebase` is the explicit local-history repair path for the common
case where GitHub merges have been fetched and the local stack still contains merged
changes.

UX is explicit:

- without `--dry-run`, it performs only rebase steps whose destination is `trunk()`
- with `--dry-run`, it previews the local rebase plan
- if a rebase step would land on another surviving change, it stops and tells the user
  to rebase manually with `jj rebase`
- if repo policy is part of the problem, it says so directly rather than making the
  user reverse-engineer it from the DAG

Its job is to restore one active local linear stack from three inputs:

- the local commit-parent path
- GitHub PR state for bookmarks
- saved tracking, including the last-submitted local `commit_id` and submitted
  topology hints for each change

It does not treat every fetched remote branch tip as local ancestry that must be
preserved. GitHub is authoritative about PR outcomes and remote branch tips, but the
local stack is authoritative about which logical changes are still part of the user's
active stack.

The algorithm:

1. Discover the local stack from the requested head down toward `trunk()`, tolerating
   immutable or divergent side revisions created by fetching merged PR branches.
2. For each logical change on that path, classify the PR as open, merged,
   closed-unmerged, or absent.
3. Treat only merged changes on the stack as removable. Open and absent changes stay
   in place. Closed-unmerged changes are not rewritten automatically.
4. For each remaining change, compute its desired new parent in logical order:
   - the nearest earlier remaining change on the stack, if any
   - otherwise the current `trunk()`
5. Rebase each remaining segment whose current parent is a merged change onto its
   desired new parent, but in the default mode only run the steps whose destination is
   `trunk()`.
6. If later remaining segments would still need to land on another remaining change,
   stop and require manual `jj rebase`.
7. Once the rebases succeed, retire each merged change whose local copy is provably
   inert: the local commit is exactly the last submitted (reviewed) commit, only one
   visible revision carries the change ID, the commit is mutable, its local bookmarks and any
   observed remote review bookmark are unambiguous, and bookmark policy allows touching every
   local bookmark still pointing at it. Retirement abandons the local copy, removes its saved
   tracking, and deletes its managed remote review branch. Local bookmark inspection uses one
   current repo-wide snapshot after rebasing, because abandoning a commit affects every local
   bookmark that targets it, not only its tracked review bookmark.
   Copies that fail the proof — rewritten since submit, divergent, pinned immutable by
   a fetched review branch, or guarded by an unmanaged bookmark — stay in place with
   an action explaining why. Retirement deletes any verified managed remote branch
   first, abandons the inert local copy, and removes saved tracking last, so a failed
   remote deletion keeps the exact identity needed to retry. Plain `cleanup` retires
   immutable leftovers once their tracking goes stale. A conflicted local or remote review
   bookmark preserves the local copy and tracking until its identity is resolved. Change-id
   headers that newer
   `jj` transfers through Git are
   deliberately not part of the proof: forge squash and rebase merges drop them, so
   the saved last-submitted commit is the reliable evidence.
8. Do not rebase surviving local descendants onto fetched branch-tip commits for
   merged non-trunk PRs. Those fetched commits are review-branch state, not the
   canonical continuation of the local stack.

This keeps the local result as close to linear as possible:

- merged changes drop off the active path
- surviving open changes stay in order
- unsubmitted local work above them stays attached to the nearest surviving base
- fetched side copies may remain as stale artifacts but no longer define the active
  stack

`cleanup --rebase` only stops when it cannot prove what the stack means. In particular,
it stops with a targeted diagnostic when:

- the stack itself is not a supported linear walk
- a stack change has an ambiguous PR link
- a merged stack change has local edits since its last submit, and removing it would
  discard unpublished work
- a closed-unmerged stack change would need to be skipped or removed (that is user
  intent, not automatic cleanup)

It does not stop merely because fetched GitHub merges created extra visible revisions
or moved PR branches to merge commits.

### Landing and merge lifecycle

`jj stack land` is the terminal operation for a reviewed local stack, but it stays
local-stack-first and `jj`-native.

The local `jj` stack remains the source of truth. `land` does not silently repair
topology, invent ancestry from GitHub, or treat PR branches as the canonical landed
history.

Default UX is mutate-by-default with `--dry-run` available:

- without `--dry-run`, `land` computes the landing plan from the current local stack
  and current GitHub state, then performs the landing and any follow-up bookkeeping it
  can already prove safe
- with `--dry-run`, it prints the landing plan, the landable changes, the target trunk,
  and any follow-up bookkeeping it can prove safe
- `--pull-request <pr>` is an alternate selector for the linked local change; `land`
  then considers the consecutive path from `trunk()` through that change, not just the
  one PR in isolation

The landing unit is one precise thing: the consecutive changes from `trunk()` that can
be landed now. That means:

- the stack's `base_parent` must equal the resolved `trunk()` before any mutation; if
  the stack forks from an older trunk ancestor or sits on a merged side-branch, refuse
  with a targeted local diagnostic pointing at `cleanup --rebase` or plain `jj rebase`
  rather than force-moving the local trunk bookmark sideways
- walk the local stack upward from `trunk()`
- stop immediately if any change still has unresolved conflicts
- by default, include consecutive changes whose PRs are open, not draft, approved, and
  whose link is unambiguous
- stop at the first merged, closed-unmerged, missing, ambiguous, draft, conflicted,
  changes-requested, or not-yet-approved change
  - if none of those changes can be landed, say so directly

`land` may also offer an explicit readiness-bypass flag for users who want to preview or
apply the open prefix anyway, but the bypass stays narrow:

- it may bypass readiness checks such as draft or review-decision state
- it must not bypass ambiguous or missing PR linkage
- it must not bypass trunk push protection or other integrity checks

This is intentionally not "the entire stack no matter what" and not "whatever open PR
the user typed". It keeps the command aligned with the local DAG and avoids
partial-stack guesses.

When the local commit for a landable change no longer matches the corresponding
`review/*` branch tip on the remote, `land` classifies the drift:

- if the local diff against the local parent is byte-identical to the remote commit's
  diff against its parent, the rebase is tree-equivalent. `land` refreshes each
  affected `review/*` branch to the rebased commit as a pre-land step, announces the
  refresh, then performs the normal trunk transition so reviewers looking at the
  closed PR see the commit that actually landed.
- otherwise the local tree diverges from what was reviewed. `land` refuses and points
  the user at `submit` so the PR can be updated and re-review requested before
  landing.
- `--dry-run` describes the planned refresh but does not push.

The auto-refresh only covers pre-land state synchronization. `land` does not mutate
review content, rewrite history, or bypass readiness checks in the process: after the
refresh push, it re-verifies the PR approval state before touching trunk, so repo
policies that dismiss approvals on push abort the landing before trunk moves, leaving
the refreshed branch in place for the user to re-request review and retry.

Per the recommended GitHub policy, `review/*` PRs are review-only and not merged on
GitHub directly. `land` therefore replays the landable changes onto the trunk branch
locally in `jj`, preserving them as a stack of commits rather than collapsing into one
squashed trunk commit, then updates the trunk branch by pushing the new trunk tip with
an optimistic lease that respects branch protection. Trunk protection and required
checks gate landing; `review/*` protection only exists to block accidental direct
merges of review branches.

That direct trunk push is the default landing transport. `land --via merge` is the
alternative for repos where trunk cannot be pushed directly at all (branch protection
that requires PRs, required checks, merge queues): instead of replaying commits
locally and moving trunk itself, `land` finalizes each landable PR bottom-up on
GitHub — retargeting its base to the resolved trunk branch when needed, then merging
it through the pull request merge API. This never merges a PR whose base is a
`review/*` branch: the retarget to trunk always happens first, which is exactly the
shape the recommended policy allows. The readiness scan, conflict checks, drift
classification, and pre-land review-branch refresh are identical across transports.

The merge method comes from `--merge-method <merge|squash|rebase>`; without the flag,
`land` uses the repository's allowed merge methods when exactly one is enabled and
otherwise stops and asks for an explicit choice. A rebase merge is refused when more
than one PR is being landed: GitHub rewrites commit IDs during a rebase merge, so
every later PR in the prefix would replay its ancestors' commits.

If GitHub reports a PR as not mergeable — pending required checks, new conflicts, or
repo policy — `land --via merge` stops fail-closed at that PR: changes already merged
below it stay merged and recorded in tracking, nothing above it is touched, and the
diagnostic says to make the PR mergeable and rerun. Merging on GitHub does not move
local history, so after a merge-transport landing the local stack still contains the
merged changes; `sync` (or `cleanup --rebase` plus `submit`) is the follow-up that
rebases the survivors and refreshes their PRs. The operation-log trunk-push resume
carve-out does not apply to this transport: a rerun sees the already-merged changes as
merged ancestors and points at the same `sync` follow-up.

Recovery guidance stays case-specific:

- if the PR link is missing or ambiguous, point at `view --fetch` and `relink`
- if the landing scan stops at a closed-but-unmerged PR, say so directly and tell the
  user to close or clean up that stack before retrying
- if the scan stops at a draft, unapproved, or changes-requested PR, say so directly
  without suggesting an override flag
- if the stack needs local ancestry repair after an earlier merge, point at
  `cleanup --rebase`
- if the stack has no changes that can be landed now, say so directly and explain
  whether the user should pick a different head, clean up merged ancestors, or repair
  closed PR state first
- if repo policy or branch protection blocks the trunk transition, surface that as a
  hard error rather than trying an alternate mutation path. GitHub marks these
  rejections with stable codes (`GH006` for classic branch protection, `GH013` for
  rulesets) but prose reason lines, so `land` classifies the reason only to choose
  the hint: pending required checks point at waiting for the review-branch checks
  and rerunning `land`, a pull-request requirement points at `--via merge`, a
  merge-queue requirement says the queue must merge the PRs, and an authorization
  failure names repo access. The raw rejection lines stay in the error, an
  unrecognized reason falls back to them alone, and classification never changes
  what the command does

`land` only owns the bookkeeping that follows directly from the trunk transition:

- before a direct trunk push, durably save one typed pending transaction containing the
  exact GitHub repository, remote, trunk before and after the push, planned commit and
  change IDs, review bookmarks, and PR numbers
- never start a second direct-push transaction while one is unresolved; first reconcile
  the saved transaction against the current remote trunk. This recovery runs before
  selector resolution or normal stack discovery, so unrelated local topology cannot block
  completion of an already-applied trunk transition
- if the saved commits did not reach trunk, restore a local trunk bookmark moved by the
  interrupted attempt, clear the unapplied transaction, and replan from current state
- if the saved commits reached trunk, require the current GitHub repository, remote, trunk,
  PR head branch and commit, and every review-branch target to match the checkpoint before
  resuming. A review branch must still exist even after its PR finalized, and the PR head commit
  is checked again on each finalization load; external drift fails closed instead of closing or
  retiring a different review
- checkpoint finalization progress together with the per-change tracking state so a rerun
  can repeat remote mutations idempotently after any interruption. A requested PR close only
  counts as finalized after a reload confirms that the PR is no longer open
- after every landed PR finalizes, atomically clear the pending transaction and retire the
  direct-push landed tracking in one durable state replacement; an audit-log append is not
  part of this commit point
- keep the operation log observational. Missing, stale, or malformed trailing audit data
  must not change whether a direct-push transaction is recoverable
- close or mark landed only the PRs that correspond exactly to the landed changes,
  once the trunk transition succeeds
- apply that PR finalization bottom-to-top through the landed changes so GitHub-side
  state changes follow the same stack order as `submit` and `land`
- forget the local `review/*` bookmarks for the landed changes, but only when those
  bookmarks still point at the landed commits; `--skip-cleanup` retains them for
  explicit repair or inspection
- if there are surviving descendants above the landed changes, tell the user to repair
  local ancestry with `cleanup --rebase` and rerun `submit`. `land` does not silently
  retarget or rebase surviving descendants

`land --via merge` keeps the merged tracking records until the follow-up `sync` or
`cleanup --rebase` has used them to remove GitHub-merged ancestors from the local stack.
The direct-push transport does not need that follow-up state because the landed commits
are already the trunk commits.

Broader cleanup remains the job of `cleanup`:

- pruning saved entries outside the landed changes
- deleting stale PR branches or stack-summary comments not proven to belong to the
  just-landed changes
- removing fetched side copies
- any ambiguous or indirect repair that still needs user confirmation

## Tracking-state file format

The file is JSON, validated through typed models. TOML is reserved for human-authored
config. If the file is unreadable or partially written, treat it as missing for recovery
purposes, warn once, and fall back to rediscovery where safe. Deleting the file does not
break the stack model, though it may force rediscovery or manual reattachment of
bookmarks.

Shape:

```json
{
  "version": 1,
  "pending_direct_land": null,
  "changes": {
    "<full-change-id>": {
      "bookmark": "review/fix-bookmark-resolution-ypvmkkuo",
      "unlinked_at": "2026-03-22T12:34:56+00:00",
      "link_state": "active",
      "pr_number": 123,
      "pr_review_decision": "approved",
      "pr_state": "open",
      "pr_url": "https://github.com/org/repo/pull/123",
      "navigation_comment_id": 456789,
      "overview_comment_id": 456790,
      "last_submitted_commit_id": "0123456789abcdef",
      "last_submitted_parent_change_id": "zzzzzzzzzzzzzzzz",
      "last_submitted_stack_head_change_id": "yyyyyyyyyyyyyyyy"
    }
  }
}
```

Config goes under `[jj-stack]` in the standard `jj` config scopes
(`jj config edit --user|--repo|--workspace`), for example:

```toml
[jj-stack]
reviewers = ["octocat"]
labels = ["needs-review"]
```

Bookmark-selection patterns such as `use_bookmarks` belong in config, not in the
tracking-state file.

## Current scope

Supported:

- one remote
- one GitHub repo target
- linear stacks
- visible mutable changes
- one PR per reviewable change

Rejected:

- merge commits inside the review chain
- divergent changes
- stacked reviews that cross repos or remotes
- bookmark naming collisions caused by matched or generated names

## Open questions

1. Should the tool eventually pass richer structured context to `--describe-with`
   helpers, or stay limited to `--pr <revset>` / `--stack <revset>` and JSON stdout?
2. Should abandoned or split PRs be auto-closed, or only surfaced as cleanup
   suggestions?

## Bottom line

The central insight is simple:

In a branch-first review tool, stack metadata often becomes part of the core model. In
`jj`, the stack model is already the commit DAG. The tool's job is just to map that DAG
to GitHub's branch-based PR API with stable bookmarks.

## References

The design above relies on a small set of `jj` concepts and docs:

- `docs/glossary.md` for `change_id`, bookmarks, rewrites, and visible commits
- `docs/bookmarks.md` for bookmark behavior, tracking, and push safety
- `docs/github.md` for the current GitHub workflow and `gh` caveats
- `docs/config.md` for generated bookmark names on `jj git push --change`
- `docs/templates.md` for machine-readable template output
- `docs/FAQ.md` for guidance on integrating with `jj`
- `docs/technical/architecture.md` for why `.jj` internals should not be treated as an
  external extension surface
