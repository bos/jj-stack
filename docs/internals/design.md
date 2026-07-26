# jj-native stacked GitHub review: design

Status: canonical product specification. This document is the sole behavioral authority for
`jj-stack`. Implementation structure belongs in `implementation-strategy.md`; evidence policy
belongs in the testing and review documents; deferred questions belong in `backlog.md`.

## Safety rules, in priority order

These rules are ordered. A lower rule never justifies weakening a higher one.

1. Never lose, silently abandon, or unexpectedly publish local work.
2. Never mutate a repository, remote ref, or pull request without exact current identity and the
   applicable lease or expected-head guard.
3. Never guess ambiguous linkage or silently adopt a replacement review.
4. Merge only the exact snapshot submitted for review, and re-check its identity and head
   immediately before every irreversible mutation.
5. Default commands affect only selected review identities. Repository-wide mutation requires an
   explicit command mode.
6. Recovery removes or replaces the saved PR and branch identity and exact submitted commit only
   through an explicit identity-changing command or after remote-result and dependent-stack checks
   make removal safe. Leftover cleanup must not block unrelated useful work.
7. Every fail-closed state and cleanup warning names a command the user can run next.

The `jj` DAG is the only source of local topology and content truth. Only ancestry from the
fetched trunk commit for the configured remote proves that reviewed work landed, under the
exact-commit and merge-result rules below. GitHub is authoritative for PR identity, lifecycle,
reviews, live native-resource membership and transitions, and merge-result identity. Local
tracking stores only the repository, PR, and branch fields needed to avoid acting on a different
review, plus the exact commit last submitted. The only additional durable policy fact is whether
one local/GitHub repository pair supports native GitHub stacks. It never caches permission, stack
topology, or merge-operation state.

## Summary

`jj-stack` turns a linear chain of `jj` changes into a stack of GitHub pull requests
without making side metadata the source of truth.

The model is small:

- one reviewable unit is one visible mutable `jj` change, identified by its full `change_id`
- one stack is a linear chain of those changes from a chosen head back to `trunk()`
- each tracked change gets one stable remote branch, used as that change's PR head
- the local stack is rediscovered from the `jj` DAG on every run, not from a saved parent map

The only per-change state `jj-stack` saves locally is the PR and branch attached to each change
and the exact commit last sent for review. It also caches one boolean per local/GitHub repository
pair for native stack support. Everything else is observed or derived. That keeps the tool feeling
like an extension of `jj` rather than a parallel stack manager.

## Recommended GitHub policy

The repository should protect trunk and allow the merge methods its maintainers intend to use.
Review branches are transport branches, not alternate integration branches. Users should ask
`jj-stack merge` to merge a reviewed path rather than merge an intermediate stacked PR directly.

`jj-stack` does not duplicate repository policy. It does not preflight approvals, checks,
conflicts, merge queues, or auto-merge state across the repository. GitHub applies those rules to
the requested native stack or ordinary PR mutation, and `jj-stack` reports the result.

## Design goals

1. Make stacked GitHub PRs feel native in a `jj` workflow.
2. Be easy to use.
3. Avoid out-of-band metadata as a source of truth.
4. Keep branch names stable across rewrite-heavy review.
5. Recompute as much as possible from `jj` state on every run.
6. Keep any persisted state optional, minimal, and tool-owned.

## Relevant `jj` constraints

A few `jj` properties drive this design:

- GitHub review is still branch-based. Even in a `jj` workflow, GitHub wants a head branch
  and a base branch per PR.
- Review branches need not remain in the local `jj` view. The backing Git store can observe and
  mutate exact remote refs while local topology remains entirely in the DAG.
- Ordinary jj bookmarks still behave normally. `jj-stack` reserves `review/*` for its remote-only
  transport branches and rejects locally imported names in that namespace.
- `change_id` is the durable logical identity of a change across rewrites. The commit ID
  is not.
- Both `jj-lib` and the CLI are moving integration surfaces; this tool keeps its
  assumptions narrow.
- `jj`'s internal storage is not an extension API; the tool does not write into `.jj/`
  internals.

## Mental model

### Review change

A review change is one visible mutable `jj` change, identified by full `change_id`. That is the
durable identity — not the commit ID, not the remote branch name, not the current diff base.

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

If an ancestor on the chain has other reviewable children, those are separate PR chains
and out of scope for the current command unless the command explicitly asks about more
than one stack.

`jj` can model all those shapes; GitHub's stacked-PR UX gets much harder once the unit
is no longer a simple parent-child chain.

### Pull request branch

Each tracked review change gets exactly one remote Git branch, used as the GitHub PR head. The
branch name is readable to humans and stable for tooling. `jj-stack` reserves the fixed `review/`
namespace; branches outside the complete managed grammar cannot be adopted. Managed review
branches do not persist as local jj bookmarks.

The initial name is built from:

- the fixed `review/` prefix
- a lowercase ASCII slug from the first line of the commit description
- an eight-character `change_id` suffix

```text
review/<slug-from-subject>-<change_id.short(8)>
```

Example:

```text
review/add-cache-index-ypvmkkuo
```

The slug helps reviewers using the GitHub UI or plain Git. The `change_id` suffix keeps
the name tied to the logical change without becoming noisy. Eight characters is fixed,
readable, and effectively unique once combined with the slug. If two resolved names collide,
`submit` stops; a never-submitted change can use a different subject to produce a different
initial slug.

The slug is only an input to the *initial* default name. Once a review is created, its branch is
not automatically renamed when the commit subject changes — title churn must not cause branch
churn during review.

In other words: generate once, then pin. For a tracked change, `ReviewIdentity.head_ref` is the
only branch-name authority; ordinary commands never rename or replace it from discovery. For an
untracked change, initial submit generates the default name. After an interrupted first submit,
recovery may reuse exactly one matching remote branch only when its suffix, full change-ID commit
header, expected target, and absence of an existing PR jointly prove it came from that attempt.
Zero or multiple candidates cannot establish identity.

If two changes resolve to the same branch, `submit` stops before mutating anything.

Ordinary fetches exclude `refs/heads/review/*` through the selected remote's Git refspec. An
effective jj `remotes.<remote>.fetch-bookmarks` override would bypass that isolation, so commands
stop and name the setting to unset. Any bookmark in the reserved namespace already imported
locally also stops commands with explicit guidance to move any work aside, forget the bookmark,
and export the updated jj view. The reservation and this check cover the same namespace on
purpose: an untracked remote bookmark anywhere under `review/` makes its target immutable, so a
name test narrower than the fetch exclusion would leave that state with no diagnostic. Every broad
jj import or fetch that jj-stack performs repeats the check before its result is used. If such an
operation exposes a reserved-namespace ref that existed only in the backing Git store, the same
diagnostic stops the command before the bookmark can affect attachment or stack discovery. These
are environment diagnostics, not a second source of branch identity.

### Review base

The GitHub base branch for a review change is:

- the parent review change's remote branch, if the parent is also being reviewed
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
- whether a remote review branch needs to move after a rewrite

All of that already lives in the commit DAG, the change-ID model, tracking identity, and direct
remote observation.

### Stored in the tracking-state file

Tracking contains two distinct versioned records keyed by full `change_id`:

- `ReviewIdentity` version 2: GitHub host, repository owner/name, PR number, and one canonical
  head owner/ref
- `SubmittedBaseline`: the last successfully submitted `commit_id` for that identity

The identity's head ref is the pinned review branch name. No other field may act as a second
branch-name authority. Only review creation, `relink`, `submit --restart`, checkout/bootstrap,
`unstack --local`, or verified retirement after dependency checks may change an identity. Plain
`unstack` retains the identity and baseline after closing the PR. Ordinary `submit` may update
only `SubmittedBaseline`, after verifying that the live PR matches the saved
`ReviewIdentity` and its head SHA equals the current local commit being recorded. `view` and
`list` never change either record.

A live PR matches `ReviewIdentity` only when its GitHub host, repository, PR number, and head
owner/ref equal the saved fields. A check for the exact reviewed snapshot additionally requires
the live PR head SHA to equal `SubmittedBaseline.commit_id`.

PR lifecycle, draft state, review decision, URL, comments, readiness, merge result, submitted
parent/stack pointers, landed state, and whether cleanup is safe are observed live or computed for
output rather than stored in tracking.

The same state-file envelope contains `stacked_pull_requests`, a boolean map keyed by the resolved
GitHub host, owner, and repository. The enclosing state path supplies the local repository half of
the key. An absent entry means detection has not completed; it is not a third capability value.
The first command that needs the distinction calls GitHub's stack-list endpoint and saves `true`
on success or `false` on a conclusive `404`. Other failures save nothing. There is no automatic or
explicit redetection. Dry-run may probe but never saves the result.

The commands that need the distinction are non-empty `submit`; `merge` and remote `unstack` with
saved PR identities; cleanup before deleting a known PR branch; selected `sync` when terminal
landed evidence or unexplained review-branch drift may involve native history; and `sync --all`
before mutating an eligible review. A cached `false` uses navigation comments without another
stack API read. A cached `true` never changes implementations mid-command: a required membership
read that returns `404` fails without changing the cached value.

A change can be in one of two tracking states:

- **untracked**: no record yet. Predicted branch names and remote observations alone do
  not count as tracking.
- **tracked**: a `ReviewIdentity` record exists; the tool inspects the exact saved PR and
  branch. A complete submitted review also has a `SubmittedBaseline`.

PR rediscovery is an explicit recovery flow. Plain `view` does not create identity for a
never-tracked change, and ordinary commands never replace a missing, closed, moved, or ambiguous
review automatically. They preserve the saved identity and name `relink` or `submit --restart`.

User-authored settings such as reviewer or label preferences live in `jj` config, not in the
tracking-state file.

Managed comments are derived output, not a source of truth. In a repository without native GitHub
stack support, `submit` regenerates navigation comments from the current `jj` stack. In every
repository, explicit or helper-generated stack prose is stored in one overview comment on the
selected head PR. `submit`, `unstack`, and `cleanup` may read comments to re-find or delete
comments the tool previously wrote, but `view` does not inspect issue comments.

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

Mutating commands take a repo-scoped advisory operation lock in that same state directory before
reading or writing state. The lock serializes cross-command mutation; its companion file records
the owning command, PID, and start time for diagnostics. `list` does not take the lock. Read-only
inspection does not write tracking observations.

The operation lock only serializes concurrent processes. Commands do not persist their planned
selection, selected parent chain, progress phase, or remaining work. After an interruption, the
next command rereads `jj`, the remote, and GitHub and computes what remains.

## Submission algorithm

Given a chosen head revision:

1. Resolve the head. When the user runs `submit` with no `<revset>`, the head is `@-`.
   `@` stays explicit user intent and is never selected by an omitted argument. Print the
   selected head after the transient preparation status has cleared, so persistent output
   starts on its own line.
2. Follow first parents from the head to the nearest commit also reachable from `trunk()`. That
   commit is normally the parent below the review stack. If it entered trunk as a non-first parent
   of a merge, stack inspection exposes it so recovery can diagnose the landed ancestry; `submit`
   rejects that immutable boundary rather than publishing it.
3. Reject ambiguous shapes rather than papering over them with metadata. Also stop if any
   change in the stack still has unresolved conflicts: `submit` must not push a
   conflicted snapshot.
4. Resolve each change's stable remote branch using the rules from "Pull request branch" above.
5. Resolve the GitHub host and repository from the selected remote. Its fetch and push URLs may
   differ, but must identify the same repository. For every tracked change, require the live PR
   to match its saved `ReviewIdentity` before any mutation. A remote swap, repository retarget,
   renamed head, moved branch, missing PR, or replacement PR fails closed with `relink` or
   `submit --restart`; observation never rewrites identity.
6. Verify the selected remote's actual review ref and the live PR head. The remote head is safe
   only when it still equals the submitted baseline or already equals the current local commit
   after an interrupted submit pushed it. Treat that push as complete only when the live PR
   matches `ReviewIdentity` and its head SHA equals the current local commit. Any other target
   stops before remote branch, GitHub, or tracking mutation.
7. Look up GitHub PR state for those branches.
   - if the saved PR link disagrees with what GitHub reports, stop and require an
     explicit recovery flow rather than silently creating a replacement PR. This
     check covers every change in the selected stack and completes before any remote branch or
     GitHub mutation, so a mid-stack link failure cannot leave sibling changes half-submitted.
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
   - in repositories without native GitHub stack support, every PR in a multi-change stack gets
     one navigation comment listing every PR top-to-bottom with a trunk line beneath the
     bottom-most PR. The current PR's title is bold and marked "this PR"; the other titles link.
     Native repositories do not classify, reject, create, update, or delete navigation comments,
     including comments left from an earlier submission.
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
8. Resolve the cached native-stack capability, detecting it only when absent. When support is
   enabled, read current repository membership and derive one executable action before local or
   remote mutation:
   - `none` when the active native membership already matches, or the final review has one PR and
     overlaps no resource
   - `create` when at least two final PRs overlap no resource
   - `append` when one resource's active suffix is an exact ordered prefix and only new top
     members remain
   - `replace` for reorder, removal, insertion below the top, or any base mutation of a native
     member. Re-read and unstack the resource's complete active suffix before changing branches
     or bases; a retained historical merged prefix is valid.
   Every active PR in an overlapping native GitHub stack must belong to the selected local parent
   chain, and the selection may overlap only one resource it could still have to mutate. This is
   one membership rule shared by `submit`, `merge`, selected `sync`, and `unstack`: it looks only
   for active members the selection leaves out, so a selected review GitHub no longer lists as a
   member never blocks, and a resource GitHub retains only for merged members is not an overlap to
   resolve. An unselected active member or changed membership fails before mutation, naming the
   `gh stack unstack` command that resolves it. A closed-unmerged review is rejected earlier, when
   `submit` checks the pull request it discovered for that change.
9. Treat proven landed ancestors as no longer reviewable. Build the complete desired remote-ref
   set for the remaining changes, directly observe every current ref at the fetch URL, then
   reobserve the whole set immediately before mutation. A tracked branch may move only from its
   submitted baseline, its already-completed interrupted-submit target, or another exact target
   proven by the recovery rules. A new branch requires expected absence. Any mismatch stops the
   entire update rather than taking over the branch.
   - treat topology changes as meaningful even when the diff is unchanged: if the parent review
     change, remote target, or PR base changed, this is not a no-op
   - before pushing rewritten review branches, protect existing open PRs from GitHub's
     reachability-based close/merge behavior: for every open PR whose head ref is in
     the planned push set, simulate the post-push commit IDs of head and base; if the
     post-push head is reachable from the post-push base, first retarget that PR to
     the resolved trunk branch so the push lands without GitHub seeing a head fully
     contained in its base. The simulator resolves a base ref through the push set
     first, then through a direct observation of the push remote. When neither is
     available, the predictor skips that PR rather than guess; the post-submit closure
     check below is the catch-all for anything the predictor cannot model
   - send the complete changed-ref set in one direct `git push --atomic` to the push URL, with an
     exact `force-with-lease` expectation for every ref. There is no sequential fallback,
     persistent local review bookmark, or post-push fetch
   - process PR creation and updates bottom-up after that atomic branch transition
   - compute the GitHub base branch:
     - the nearest still-open ancestor PR in the chain, if any
     - otherwise the resolved trunk branch
   - if an ancestor PR has merged but the local parentage still reflects the old review stack,
     stop and point at selected `sync`; `submit` does not guess how rewritten merge results map
     back to the local DAG
   - create or update the PR for `head branch → base branch`
   - creating a review writes its `ReviewIdentity` and `SubmittedBaseline` together
   - updating a review advances only its baseline after the live PR matches the saved identity,
     its head SHA equals the current local commit being recorded, and the push succeeded or an
     interrupted push was recognized under step 6
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
10. For native `create` or `append`, list repository stacks again and require the same action and
    affected resource before applying one complete mutation. Creation sends the complete desired
    membership; append sends only the new ordered top members. Supporting native stacks of 100 or
    more reviews is out of scope: there is no batching, size-specific recovery, or local
    size-policy check.
11. Synchronize overview comments in every repository and navigation comments only when cached
    native support is `false`, then run the existing unexpected-PR-closure verification.

The bottom-up ordering matches stack dependency order, and the parent relation is read from the
DAG, not from saved metadata. No command other than `submit` creates a PR or publishes a
never-submitted change.

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
so an open→closed transition shows that GitHub's reachability-based
auto-close fired in a way the pre-push predictor did not anticipate, and an open→missing
transition means the PR was deleted or transferred during the run. The check is
detection, not repair: it turns silent data loss into a loud error naming the affected
PRs so the operator can reopen or restore them on GitHub. Defense-in-depth for the
predictor, not a substitute.

For a stack with exactly one change, `submit` behaves like a plain PR-submit flow: no stack helper
invocation and no new navigation or overview comment. Repositories without native GitHub stack
support remove older managed navigation and overview comments left from a larger selected stack.
Native repositories leave navigation comments untouched and remove only an obsolete managed
overview. After a successful live submit, the URL of the top of the stack is printed so the user
can open it in a browser.

There is no meaningful stack metadata to add when the stack has only one PR.

## Recovery and repair

When review identity is unclear, `jj-stack` is conservative.

If `submit` cannot prove that a change still corresponds to the same review branch and
PR, it stops with a targeted diagnostic rather than guessing. It does not silently open
a new PR just because a saved link, branch, or GitHub state is missing or damaged.

The recovery surface is explicit and narrow:

- `jj-stack view --fetch [<revset>]` refreshes ordinary fetched repository state and directly
  observes saved review branches before inspecting GitHub PR state. It reports the stack and
  saved PR state without mutating GitHub or importing managed review branches
- `jj-stack relink <pr> <revset>` is a repair command. It explicitly reattaches an
  existing PR (and its same-repo head branch) to a specific `jj` change. It directly observes the
  exact branch, reads the remote commit object's full change ID without creating a ref, and saves
  the PR identity and exact remote target as the submitted baseline. Replacing any stale saved
  baseline is what lets a later `submit` update the relinked review rather than rejecting that
  branch or opening a replacement.
- `jj-stack submit --restart <revset>` replaces stale or unusable reviews while keeping the
  selected `jj` changes. It derives each replacement branch deterministically from the saved
  head ref, old PR number, and change ID. It retains every old identity and baseline until the
  whole selected replacement stack has passed a fresh joint check and can be saved in one write.

Selector defaults are listed once under "CLI shape" below. The principle: lifecycle
commands default to the stack headed by `@-`; `relink` requires one explicit `<revset>`; `@` is
always explicit user intent and is never selected by an omitted argument.

### `view`

`jj-stack view [<revset> ...] [--pull-request <pr> ...]` shows the local stack(s) and
any locally known review identity for them.

It is local-first. If a change has never been locally attached to review, `view`
reports it as not submitted and does not query GitHub for speculative PR matches based
only on predicted branch names or remote observations. It does not create local tracking for a
never-tracked change.

`jj-stack view --fetch [<revset> ...] [--pull-request <pr> ...]` is the same command,
but it refreshes ordinary fetched state and directly observes saved remote review refs before
checking already-known GitHub PR state. Ordinary fetch continues to exclude `review/*`.

When more than one selector is given, `view` inspects them in command-line order,
suppresses exact duplicate stack reports, continues past selector-local resolution
failures, and exits with the incomplete-report code if any individual stack would have
done so. A single selector behaves like bare `view`: a failure that prevents any report
propagates with its category code (for example, unsupported stack shape exits `2`)
instead of degrading to the incomplete-report code, so the exit code for a drifted state
does not depend on whether the selection was explicit.

Fetched repository state can expose extra visible revisions for merged changes, so `view` does
not insist that every visible revision still forms one supported review stack. It walks the
parent chain, tolerates immutable or divergent side copies exposed through fetched history, and
reports the stack revision for each logical change.
If a merged PR still appears on the stack, `view` continues and surfaces that row as
"cleanup needed" rather than calling the stack broken. If the local history no longer
has any supported linear walk after refresh, `view` stops with a targeted diagnostic
rather than a traceback or an unadorned subprocess error.

Unlike `submit`, `view` may fall back to local-only output when the repo is not
configured well enough to resolve a remote or GitHub target. Default output stays
concise — one effective summary per change rather than dumping saved and remote
diagnostics inline.

With `--json`, `view` prints a structured version of that same per-change summary. The
payload includes the selected stacks, their changes, saved review branch names, PR identity,
and concise review status. It does not expose cache state, raw remote branch targets,
or saved tracking records; command failures and incomplete inspection still use stderr
and the process exit status. The machine-readable schema for the public output lives in
[`docs/json-output.schema.json`](../json-output.schema.json).

`view` may add a repo-level advisory for other tracked stacks when a tracked change's
`SubmittedBaseline.commit_id` differs from its current commit. The advisory names the stack heads
and points the user at running `view` on each, because the correct follow-up depends on the cause.
Topology and stale comments alone do not trigger the advisory.

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
- when only saved PR identity is available, labels it as tracking data rather than implying a
  live lifecycle result
- does not inspect managed navigation or overview comments. Those comments are derived review
  artifacts, and the commands that create or delete them own their validation.
- never writes `ReviewIdentity` or `SubmittedBaseline`; live observations may enrich this report
  but cannot change saved identity or authorize later mutation

When `view` reports `cleanup needed`, it explains why in plain language:

- a merged PR still appears on the local stack
- descendant `submit` operations will keep following that old ancestry until the user
  repairs it
- the next command is the exact selected `jj-stack sync <change-id>`; add `--dry-run` first to
  inspect the planned stack update before mutating local history

User guidance names the command and its effect rather than exposing internal classifications of
fetched copies and selected revisions.

### `list`

`jj-stack list [--fetch]` gives one repo-scoped summary row per locally known stack. It
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
`unstack --cleanup --pull-request orphans` to attempt to close and clean up every orphan shown.
The explicit single-PR closure path remains `--pull-request <pr>`. Without this surfacing, common
workflows (squashing two reviewed changes by emptying one and abandoning it) would leave PRs open
without the user noticing.

With `--json`, `list` prints the same row model as the text table. Stack rows include
their changes so clients can derive stack length, head change, and PR list directly
from the structured changes. Orphaned PRs remain rows with `type: "orphan"` rather than
a separate internal bucket. The same schema file covers both `view --json` and
`list --json`.

These commands are not sources of truth and do not reattach identity. They inspect a
`jj`-derived stack after damage, cross-machine work, or manual edits on GitHub; explicit
`checkout`, `relink`, or `submit --restart` own reattachment.

### `checkout`

`jj-stack checkout [--fetch] [--pull-request <pr> | --revset <revset>]` resolves one
exact stack and sets up tracking for it. It does not mutate GitHub.

`checkout` is the explicit recovery and bootstrap path for review state that already
exists remotely. If a stack already has PRs on GitHub but local tracking is missing on
this machine, `checkout` is what you run. Plain `view` does not do this implicitly.

Selector handling stays unambiguous: a bare positional argument does not double as both
revset and PR number, and omitting selector flags defaults to the stack headed by `@-`.

`checkout --pick` is a third, interactive selector: it lists the locally tracked stacks
(current stack first) numbered on standard output, reads one number from standard
input, and then proceeds exactly as if that stack's head had been passed via
`--revset`. The picker offers only stacks that already have local tracking — attaching a stack
that exists only on GitHub still requires an explicit `--pull-request`. Empty, non-numeric, or
out-of-range input fails closed with a usage error, and no tracked stacks at all is a targeted
error pointing at `--pull-request`. The prompt happens before the operation lock is taken so an
idle picker never blocks other commands.

`checkout` sets up tracking, not workspace motion:

- without `--fetch`, use only commits and PR-backed state already available locally
- resolve from an explicit PR or an explicit local stack
- for an explicit PR, require the configured repository, complete managed branch grammar, exact
  PR base chain, and unique PR head claims to identify one stack
- with `--fetch`, refresh ordinary repository state, directly observe the selected top review
  ref, and import only that exact ref through a fixed temporary Git ref
- verify the imported DAG against the exact PR chain and every PR's directly observed remote
  target, then remove the temporary jj bookmark and Git ref before continuing
- refresh the tracking entry only for that exact stack
- when `--fetch` pulls in a remote-selected stack, print the fetched tip rather than
  changing the workspace

`checkout` does not:

- rewrite commits
- restack descendants
- check out the fetched stack into the current workspace
- open, close, or mutate PRs
- delete local history
- leave managed review bookmarks or temporary import refs behind

Failure guidance stays specific:

- if the PR head revision is unavailable locally, point the user at `checkout --fetch`
- if the PR head branch is missing on the remote, cross-repo, or ambiguous, stop and
  explain that the stack cannot be connected safely
- if multiple PRs match the same head branch, point at `view --fetch` and `relink`
- if any checked-out revision lacks an exact discovered remote branch, stop rather than inventing
  a local match
- before `--fetch` imports anything, read the selected PR head's change ID from the remote object
  without creating a ref. If a visible local revision already holds that change at another
  commit, stop and name `relink`, because importing would leave a divergent copy that no rerun can
  remove. If the change is already divergent, name the revisions to reconcile instead
- if the fetched stack shape is unsupported locally, point at selected `sync` only when the issue
  is proven landed ancestry rather than remote identity
- if `checkout` defaulted to the current stack and that stack has no matching PR, say so
  rather than silently doing nothing
- if a stale saved entry disagrees with a freshly fetched link, the fetched link wins
  only when it is exact and unambiguous; otherwise stop and surface the conflicting
  identities rather than partially overwriting

`view --fetch` stays the read-only refresh path; `checkout` explicitly sets up local tracking.
Selected `sync` updates one current stack, while `sync --all` is the separate explicit
repository-wide recovery mode.

### `sync`

`jj-stack sync [--dry-run] [<revset>]` observes and updates one selected stack:

1. Fetch the configured remote and resolve current trunk.
2. Re-resolve the selected stack from the current DAG; never replay an earlier selection.
3. Classify landed ancestors through the "Exact submitted commit on trunk" or "Selected PR's
   rewritten merge result on trunk" rules below.
4. Stop before rewriting if removing an ancestor would discard unpublished local work, if the
   remaining changes are nonlinear, or if an unreviewed change sits between reviewed changes.
5. Rebase only the selected remaining changes onto fetched trunk. When a native merge rewrote
   the active suffix, transiently import its freshly verified exact top, validate the full active
   change-ID and first-parent chain from trunk, adopt that chain, and rebase only trailing local
   descendants onto it. Atomically advance the adopted suffix's baselines before the final branch
   recheck so a later retry can distinguish those GitHub-reported snapshots from unpublished
   edits. Trailing unreviewed work remains local and sibling stacks are untouched.
6. Update only existing selected reviews. Never create a PR or publish a never-submitted change.
7. Remove tracking for a landed change only after survivor updates succeed and no other visible
   stack still needs it; otherwise report each dependent head and its exact selected `sync`
   command.

`sync` does not rebase onto newer trunk merely because trunk advanced; explicit `jj rebase` owns
that workflow. With `--dry-run`, it prints the landed classification and any cleanup or rebase
without applying them. When a rebase is required, `sync` cannot compute the later PR-update plan
until the rebase has been applied.

`jj-stack sync --all [--dry-run]` is the only repository-wide recovery mode. It fetches once,
checks every locally tracked PR, and continues past absent, malformed, obsolete, or individually
failing records. It may change only reviews whose exact submitted commit is on fetched trunk,
whose live PR matches the saved `ReviewIdentity`, and whose live head SHA equals the submitted
commit. After rechecking, it may retarget and close those landed PRs. It does not rewrite stacks,
submit work, create PRs, or treat a rewritten merge result as permission to change more reviews.
Tracking needed by visible stacks remains, and the command reports their exact selected `sync`
follow-ups.

Native resources retain merged members as a historical bottom prefix and non-historical members
as an active suffix. Closed or draft active members remain affected survivors, but they are not
merge candidates. Selected `sync` may accept survivor head and base changes reported by that same
resource only while at least one complete tracked historical member validates the transition.
This is bounded remote-result authority, not tree-equivalence evidence or permission to accept
later drift. Global sync requires a native PR to report terminally merged before finalizing it and
preserves tracked historical evidence while an active tracked suffix still needs selected sync.
An exact native member commit merely appearing on trunk does not authorize `sync --all` to
retarget and close the review.

GitHub preserves jj's Git `change-id` header through both native and ordinary rebase merges, but
not through squash merges. Selected `sync` uses the fetched result: a matching change ID is the
landed successor; otherwise it retires the old local change without relabeling the landed commit
or storing an alias.

The selector and `--all` are mutually exclusive.

### `unstack`

`jj-stack unstack [--cleanup] [--dry-run]
[--pull-request <pr|orphans> | <revset>]` ends review for one stack or an explicit set of
orphaned pull requests.

`unstack` is stack-first. It looks at the local stack, finds the open PRs the tool is
already tracking there, and either runs or previews the actions needed to end review.

In a native repository, remote unstacking must cover one resource's complete active suffix before
closing its PRs. It freshly verifies the resource before mutation. GitHub may retain the
historical merged prefix after removing that suffix; any unselected active member or incomplete
removal fails closed. `unstack --local` never consults or changes native membership.

`--pull-request <pr>` is usually an alternate selector for the local stack — it must
resolve to one linked local change. The one exception is `unstack --cleanup
--pull-request <pr>` for an orphaned PR (one whose local change has been abandoned
or otherwise dropped from every current stack): saved tracking is the only available
identity, so `unstack` acts from the exact saved PR and branch fields and still fails
closed if either is missing or ambiguous. Before deleting a branch, it verifies that
the saved PR still uses the saved branch name on the configured GitHub repository, not
just a same-named branch from another owner. A saved branch claimed by another tracked change is
ambiguous; branch deletion in that state would silently take a branch out from under another
review.

`unstack --cleanup --pull-request orphans` selects every tracked PR whose local change is absent
and which `list` reports as an orphan when the command begins. The `orphans` selector cannot be
combined with a revset or `--local`. The command processes targets in pull-request-number order
and applies the same exact PR-head and duplicate-claim checks used for one orphan. It closes a
verified open PR and cleans a verified closed or merged PR. A blocked target remains tracked and,
if open, remains open; other independently verified targets continue, and the command exits `1`
if any target was blocked. A hard failure stops the batch with prior successful cleanup
preserved. `--dry-run` performs the same selection and verification without closing PRs or
deleting review artifacts.

Without `--cleanup`, `unstack`:

- closes the open PRs the tool is already tracking for the stack
- skips already-merged or already-closed PRs rather than treating them as new close
  targets
- leaves remote PR branches in place
- retains each exact `ReviewIdentity` and `SubmittedBaseline`, so later cleanup or
  `submit --restart` can still prove what it is replacing

With `--local`, `unstack` removes only the saved local tracking records for the selected
stack: the identity and baseline pair is removed together. It does not close PRs, delete remote
branches, or inspect GitHub. The local `jj` changes remain in place. This mode is for checkouts
that should stop treating the stack as locally tracked while leaving the GitHub review stack
alone. It cannot be combined with `--cleanup`.

With `--cleanup`, `unstack` also performs conservative post-close cleanup for review
artifacts the tool can verify belong to the stack:

- delete remote PR branches on the configured remote, only when verified to belong to
  the stack
- delete managed navigation and overview comments belonging to the stack
- remove the identity and baseline pair only when no same-repository open PR still names the
  saved review branch as its base

The exact eligibility and recheck rules are defined once under
[Cleanup semantics](#cleanup-semantics). Selected cleanup works from the head toward the base, so
a dry run may treat only earlier selected PRs in that order as closed. The real command observes
GitHub again at each mutation boundary.

`unstack` is idempotent:

- rerunning `unstack` on an already-closed path succeeds as a no-op (or with a brief
  "nothing to close")
- rerunning `unstack --cleanup` after an earlier `unstack` performs only the remaining safe
  cleanup, not another close

### `submit --restart`

`jj-stack submit --restart <revset>` replaces stale or unusable reviews for the selected stack
while keeping the local changes. It requires existing complete tracking for every selected
change.

For each change it derives one retry-stable branch from the saved head ref, saved PR number, and
full change ID:

```text
review/<original-stem>-fresh-pr<old-pr-number>-<short-change-id>
```

If the saved branch already has a terminal `fresh-pr<number>` marker, the new saved PR number
replaces it rather than accumulating markers. A branch that does not match the fixed managed
grammar and the selected change's suffix cannot authorize restart.

Every old `ReviewIdentity` and `SubmittedBaseline` remains durable while the replacements are
being created. Replacement identities are staged only in memory. After every PR, native-stack
update, managed comment, and post-submit closure check succeeds, `submit --restart` jointly
reobserves all replacement PRs and branches. Each one must still name the configured repository,
unique owner/head ref, exact selected commit, planned base, and exact remote branch target. One
compare-and-swap then replaces every selected identity/baseline pair in a single state-file write.

An existing replacement branch is usable only when its remote target is absent or equals the
exact selected commit. A unique open PR already using the deterministic replacement branch is
recovered after an interrupted restart only when its repository, owner, head ref, head commit,
base, and live remote target still equal the current plan. This is a narrow retry rule, not
general PR discovery or implicit relinking. Any mismatch or ambiguous/terminal PR blocks the
restart and directs the user to inspect it and use `relink` only when it is the intended review.

Any failure before or during the final state write leaves every old pair in place. Rerunning the
same restart recovers exact replacement candidates and does not create another generation of PRs.
After a restart completes, a later explicit restart intentionally derives a new generation from
the newly saved PR numbers. A dry run applies the same candidate classification but never
mutates branches, PRs, comments, native membership, or tracking.

## Rewrite behavior

This design behaves well under normal `jj` rewrite-heavy workflows:

- **Rebase**: the commit ID changes and the `change_id` stays stable. Re-running `submit` moves
  the saved remote review branch to the rewritten commit and updates the existing PR.
- **Squash or amend**: same as rebase. If the workflow then abandons a now-empty
  change (the usual way to collapse two reviewed changes into one), Abandon rules
  apply to that change.
- **Reorder or reparent**: the stack is rediscovered from the DAG; PR base branches
  are recalculated.
- **Insert**: a new mutable change appears on the chain. `submit` opens a PR for it
  and any descendants' PR bases recalculate against the new parent.
- **Abandon**: the change leaves every current local stack and descendants reattach
  to its parent. Its PR becomes *orphaned* — surviving stacks never close, reuse, or
  retarget it. Cleanup removes its saved identity only after verifying the exact PR is closed or
  merged and its artifacts are safe to remove. An absent PR fails closed and leaves tracking for
  explicit repair or later verification. Explicit closure goes through
  `unstack --cleanup --pull-request <pr>`.
- **Split**: new logical review changes get new change IDs and usually become new
  PRs. The original keeps its `change_id` and PR and is updated normally on next
  `submit`. This is a feature, not a bug.
- **Duplicate**: the duplicate has a new `change_id` and is treated as a new
  reviewable change on whatever stack it lands on; the original keeps its PR
  untouched.
- **Ancestor merged on GitHub**: merged ancestors stop acting as review bases.
  Descendants target the nearest still-open ancestor PR, or trunk if none remain.
  Selected `sync` proves the merge result on fetched trunk and performs that local rewrite.

### Cross-stack rewrites

When a rewrite changes which stack a change belongs to, the established rules still
hold: identity is by `change_id`, each command operates on one selected stack
(defaulting to `@-`), and ambiguous linkage fails closed. Other affected stacks wait
for their own explicit command.

- **Move changes between stacks**: submitting the user's selected resulting stack
  updates that chain's PRs from the current DAG. Moved changes keep their existing
  PRs and recalculate their bases from the new parent chain.
- **Split one stack into two or more**: the resulting reviewed paths may keep common ancestors.
  When one old native GitHub stack spans more than one desired path, the user explicitly
  dissolves it with `gh stack unstack <number>`, then submits each resulting stack separately.
  Otherwise, submitting one result updates only that chain and every other result waits for its
  own command.
- **Merge two or more stacks into one**: submitting the merged stack updates every
  change on the chain bottom-up, reusing existing PRs by `change_id` and
  recalculating bases. The merged chain ends up with one overview comment on its new
  head and no internal trace of the old stack boundary.

The same applies when one rewrite affects more than two stacks.

Stacks the user has not yet resubmitted may still display old navigation or overview
comments. That is expected — `submit` does not chase comments on stacks it isn't
operating on, and `merge` does not block on stale state outside the selected stack.
`view` and `list` surface those stacks via the submitted-state rule, naming their
heads and directing the user at `view` for the per-stack next step.
Orphaned PRs left behind by a cross-stack rewrite need an explicit
`unstack --cleanup --pull-request <pr>`.

Identity and baseline left after an interrupted command are safety checks, not instructions to
resume the original selection. A later command acts on the current DAG and live remote state. It
removes those records only after proving the remote result and that no visible stack still needs
the link. Operation, selector, phase, and path are never recorded for recovery.

This is exactly the kind of rewrite-heavy flow `jj` is good at.

## Why no parent metadata

A branch-first review tool often has to remember both a named parent and an exact
parent revision because the review boundary is otherwise ambiguous after rewrites.

In `jj`, the boundary is already the commit's parent relation. The only place branch
identity still matters is at the GitHub boundary, because GitHub wants:

- one head branch per PR
- one base branch per PR

So the tool needs remote PR branches, but it does not need persistent local review bookmarks or a
saved parent graph.

## CLI shape

Built-in `--help` and the user guide own command names, aliases, and exact parser syntax. This
specification records only the enduring selection rules and effects of those commands.

`completion` is auxiliary CLI glue. It prints shell completion scripts. It is not a
review-state command and does not inspect the repo, the tracking-state file, or
GitHub.

Run with no subcommand, the executable behaves the same as `view` on the current stack.

Top-level help groups commands by intent. `--help` and `help` foreground the core
review lifecycle (`submit`, `view`, `merge`, `unstack`) plus support commands
(`cleanup`, `checkout`, `sync`, `doctor`). The repair command `relink` and shell-integration glue
(`completion`) stay hidden by default and only appear in `jj-stack help --all`. The `help`
command itself is hidden parser glue: `jj-stack help` is the same as
`jj-stack --help`, and `jj-stack help <command>` is the same as
`jj-stack <command> --help`. The default top-level help also keeps advanced global
options (`--repository`, `--config`, `--config-file`, `--debug`, `--time-output`) out
of view until `--all`.

Long command help preserves paragraph breaks so multi-paragraph guidance stays
readable.

Target selection is conservative:

- `submit`, `unstack`, `merge`, and selected `sync` default to the stack headed by `@-` when
  `<revset>` is omitted
- `submit --draft[=new|all]` and `submit --open` are mutually exclusive
- `submit --edit` and `submit --describe-with` are mutually exclusive; `--edit` composes
  with `--describe` by pre-filling the editor document from the resolved files
- `submit --reviewers` and `submit --team-reviewers` override configured reviewer
  defaults for the current invocation only. Passing either flag requests those reviewers
  even when the selected pull requests are otherwise unchanged; omitted reviewers are not
  removed
- `submit --re-request` re-requests users whose latest review is `APPROVED` or
  `CHANGES_REQUESTED`; pending review requests stay in place
- `relink` requires one explicit `<revset>`
- `sync --all` is mutually exclusive with a selector; plain `sync` defaults to `@-`
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
- `10` — `view` or `list` printed an incomplete report
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

## GitHub mutation safety

Every GitHub mutation the tool issues is enumerated below together with the
destructive default action GitHub may take in response and the in-tool defense that
prevents it. Any new mutation must be added to this list, and any without a
documented defense must either prove the destructive default does not apply or add
one before merging.

- **Push of review-branch refs** (one direct atomic Git push). When the push lands, GitHub
  re-evaluates each open PR and auto-closes (as merged) any whose head ref is now contained in its
  base ref. A reordered stack can make a stale stacked base contain a review-branch head it did
  not contain before. Defense: `submit` first directly observes every ref, plans the complete
  transition, and simulates the post-push commit IDs of every open PR's head and base refs. It
  pre-retargets any at-risk PR to trunk, reobserves every ref immediately before mutation, then
  sends one `git push --atomic` to the push URL with an exact per-ref lease. The normal post-push
  PR sync restores the final stacked bases. As a defense-in-depth backstop for cases the predictor
  cannot model, `submit` refetches PR states at the end and names any PR that transitioned from
  open to closed or missing.

- **Deletion of a remote review branch** (the same direct leased mutation transport). GitHub
  closes any PR whose head ref points at the deleted branch. Defense: branch deletion is invoked
  only by `cleanup` or `unstack --cleanup` after the exact
  [cleanup checks](#cleanup-semantics), under an exact ref lease. A failed check keeps the branch
  and identity/baseline pair.

- **`update_pull_request(base=…)`**. Setting a PR base to a branch that already
  contains the PR's head triggers GitHub's merged auto-close. Defense: in `submit`,
  base is set bottom-up to the parent change's remote branch — an ancestor of the head,
  not a descendant — and the head ref has already been pushed to its updated content.
  A native member is unstacked under an exact fresh membership check before any base
  change. In ordinary `merge`, a candidate is retargeted to trunk immediately before
  its expected-head merge request.

- **`update_pull_request(title|body)`**. The PATCH contains only fields whose values changed.
  Content-only refresh never carries `base`; a real base change carries `base` and may carry
  changed content in the same request. The previous bullet governs every request containing
  `base`.

- **`create_pull_request`**. Creating a PR with a base that already contains the
  head would trigger an immediate merged auto-close. Defense: bottom-up creation
  order means the parent's remote branch always reflects an ancestor of the new PR's
  head before the child PR is created.

- **`close_pull_request`**. Destructive by design. Defense: `unstack`, including the
  `unstack --cleanup --pull-request <n>` orphan sub-mode, acts only on explicit user instruction.
  Selected and global `sync` may close an exact landed review only after fetched-trunk ancestry
  proves the submitted commit landed and a fresh read revalidates its saved identity and exact
  submitted head.

- **`merge_pull_request`**. Destructive by design: it permanently merges the PR's
  head into its base branch. Defense: only invoked by `merge` for an ordinary PR on explicit user
  selection. Immediately before each request, reload the repository and PR, verify that the live
  PR matches `ReviewIdentity`, require its head SHA to equal
  `SubmittedBaseline.commit_id`, require the expected state and base, and pass the expected head
  SHA to GitHub. A rejection stops at that PR. Changes already accepted below it stay merged; the
  command does not rewrite local history or review branches.

- **Native stack create and append**. GitHub assigns each admitted PR to one resource. Defense:
  immediately before either mutation, list repository stacks and verify again that every active
  member of an overlapping native stack belongs to the selected local chain. Require the same
  action and affected resource, then create the complete desired membership or append only the
  ordered new top members. GitHub's mutation is the admission authority; there is no queue or
  auto-merge preflight and no fallback to comments after rejection.

- **Native stack unstack**. Removes active PRs from a native resource and can leave its historical
  merged prefix. Defense: immediately before mutation, fetch the exact resource and require its
  ordered membership to match the selected plan. Submission replacement and remote `unstack`
  cover the resource's complete active suffix; an incomplete result stops before branch or PR-base
  mutation.

- **Native asynchronous merge**. Destructive by design: GitHub atomically merges a bottom prefix
  and may rewrite active survivors. Defense: `merge` freshly verifies the identity, submitted
  head, base, and branch of every active member, then sends one request for the selected target
  with its exact head SHA. Only terminal `merged` is success. A terminal failure reports that
  nothing merged. A `409` body is decoded for diagnosis but its operation UUID is never adopted,
  because the response does not identify the target PR.

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
  site first rediscovers the comment by its managed body marker and rejects multiple matches;
  comment IDs are not tracking state. `submit`, `unstack`, cleanup, and explicit orphan cleanup
  re-verify the marker before deletion. Ambiguity leaves harmless cleanup work for later.

This list is the bar `submit`, `unstack`, `merge`, `cleanup`, and any future command
must clear before introducing a new GitHub call.

### Cleanup semantics

`jj-stack cleanup` is conservative garbage collection, never a correctness prerequisite or a
local-history repair command. Selected `sync` owns rebasing after landed ancestors.

Cleanup may remove only derived artifacts named by one complete identity and baseline pair:

- managed comments rediscovered by an unambiguous content marker
- remote managed review refs under an exact expected-target lease
- obsolete identity/baseline records only after no same-repository open PR uses the saved head
  ref as its base

Plain cleanup iterates complete tracked pairs and observes the exact saved PR number, repository,
head owner, and head ref. It batches that observation first, then falls back to bounded
one-record observations only when GitHub cannot return the batch. A matching closed or merged PR
is eligible; a matching open review is preserved. An open orphan is reported but is not closed
or cleaned by this repository-wide command. Missing PRs, lookup failures, identity mismatches,
and duplicate head claims fail closed for that record without blocking independently observable
records.

Eligibility also requires that GitHub report no open PR in the same repository whose `base.ref`
equals the saved `head_ref`. This check includes untracked and orphaned PRs. Local jj descendants
do not replace it: descendant visibility is authority for selected `sync`, not for deleting a
GitHub branch or retiring review tracking.

Immediately before deleting a branch, cleanup reloads the exact saved PR, its unique head claim,
and its open base dependents. In a native repository it also reads current membership: an active
member keeps its branch, while a historical merged member does not block otherwise authorized
cleanup merely because GitHub retains it in the resource. Comment deletion separately verifies
its managed body marker. Cleanup then rereads the exact remote ref and deletes it only when it
still equals the submitted commit, using that target as the lease. It repeats exact PR, claim,
dependent, native-membership, and remote-absence checks before retiring the identity/baseline
pair, and retires it only after all authorized artifact cleanup succeeds.

Malformed, obsolete, absent, ambiguous, or individually failing records are reported and skipped
without blocking independent cleanup work. Failed cleanup leaves safe leftovers and never changes
whether a GitHub merge succeeded. Every warning names the selected `sync`, `relink`,
`submit --restart`, explicit `unstack --cleanup`, or later `cleanup` command that can finish the
work.

`cleanup --dry-run` performs the same discovery and safety classification without mutation.

### Merge lifecycle

`jj-stack merge` is the only command that asks GitHub to merge reviewed changes. It never pushes
trunk. Cached native-stack capability plus current membership chooses the GitHub API; there is no
user-selectable transport.

The command is mutate-by-default with `--dry-run` available:

- without `--dry-run`, inspect the current local path and GitHub state, then ask GitHub to merge
- with `--dry-run`, perform the same selection, capability resolution, and validation, but print
  the planned API shape without mutation
- `--pull-request <pr>` selects the bottom-anchored prefix through the linked local change while
  still resolving the complete maximal reviewed path

After any historical merged native prefix, candidates are the contiguous open, non-draft PRs from
the bottom. The first draft or closed-unmerged PR blocks it and everything above it. With no
explicit target, choose the highest candidate. An explicit PR chooses the candidate prefix through
that PR. Approval, changes requested, checks, conflicts, mergeability, branch rules, queues, and
auto-merge are not preflight gates; GitHub evaluates them for the requested mutation.

Every affected change must identify one exact reviewed snapshot: its current local commit,
`SubmittedBaseline`, remote review ref target, and live PR head SHA are equal, and the live PR
matches `ReviewIdentity`. Diff or tree equivalence is insufficient. Any mismatch stops before
mutation and points at `submit`; `merge` never refreshes review branches, advances a baseline,
creates a PR, rewrites local history, or cleans up tracking.

The merge method comes from `--merge-method <merge|squash|rebase>`. Without the flag, use the sole
repository-enabled method. If several are enabled, stop and require an explicit choice.

When the selected reviews belong to one active native GitHub resource, `merge` sends one
asynchronous request for the selected bottom prefix:

```text
PUT /repos/{owner}/{repo}/pulls/{target_pr}/merge-async
```

```json
{
  "merge_method": "merge | squash | rebase",
  "sha": "<exact target PR head>"
}
```

The command resolves every active resource member, including survivors above a partial target,
because GitHub may retarget and rewrite them. Immediately before submitting, it reloads the exact
resource and every member's identity, submitted head, branch, and base. The target SHA is GitHub's
only caller-controlled freshness guard; lower members rely on the native group transaction rather
than one request per PR.

An accepted request returns a UUID and is polled to terminal state. Only terminal `merged` is
success; the result reports the merged PRs and final trunk SHA. Terminal `failed` reports
GitHub's reason and that nothing merged. A concurrent `409` is decoded to distinguish a matching
pending request, but its UUID is never adopted because the body does not identify the target PR.
Rerunning the same target and exact head after completion observes GitHub's terminal result.

GitHub retains merged native members as a historical prefix and may rewrite an active suffix.
That successful remote transition is reconciled later by selected `sync`; it is not an exit-1
repair phase of `merge`. Native rebase merge may merge several PRs in one request because GitHub
owns the complete transaction.

A repository without native support uses GitHub's ordinary PR merge API bottom-up. A one-PR
review in a native-capable repository also uses that API because GitHub creates no native
resource. A multi-PR review in a native-capable repository with no resource is inconsistent
submission state and points at `submit`; it does not fall back.

For each ordinary candidate, immediately reload its identity, exact submitted head, state, branch,
and base; retarget it to trunk when necessary; then call GitHub's merge API with the expected head
SHA. Stop at the first rejection. PRs already merged below it remain merged and nothing above it
is requested. Rebase merge is refused for more than one ordinary PR in a command because the
first rewrite invalidates later reviewed commit identities.

After any successful merge, the output points at selected `sync`. `sync` fetches GitHub's result,
retires proven merged ancestors, rebases only selected survivors, and updates only reviews that
already exist. It never resumes a merge request. An interrupted or partially successful ordinary
merge is recovered by running selected `sync`, then rerunning the same explicit `merge` selection
if more PRs should merge. No durable operation, rollback, compensation, or local merge repair is
stored.

Two observations prove merged work to sync:

- **Exact submitted commit on trunk:** the baseline is an ancestor of fetched trunk, the live PR
  matches `ReviewIdentity`, and its head remains the submitted commit. For a native member, the PR
  must additionally report merged before finalization.
- **Selected PR's rewritten merge result on trunk:** the exact saved PR reports merged, its live
  merge-result commit is an ancestor of fetched trunk, and its head remains the submitted commit.
  This selected-stack proof covers squash and rebase results without authorizing repository-wide
  changes.

A PR merely reporting merged, or a merge result no longer reachable from fetched trunk, permits no
destructive action. Preserve local revisions, identity, and baseline, then report the exact
selected `sync` needed after trunk is restored. Tracking is removed only after no visible
dependent stack still needs the saved link; otherwise preserve it and name every dependent
selected sync.

When a native merge rewrites the active suffix, selected `sync` adopts GitHub's exact surviving
commits instead of independently replaying the same diffs. It fetches the exact top into the fixed
temporary Git ref, validates every raw Git change ID and first parent down to fetched trunk, then
reobserves every active review branch immediately before importing the top into jj. It rebases
only trailing local descendants onto that exact top, abandons the replaced local active copies,
and atomically advances every adopted survivor baseline. It then reobserves the complete branch
set before removing the temporary ref and bookmark. A branch move at that last check preserves
the historical tracking and the adopted baseline, so retry can adopt the newer exact remote chain
without mistaking the earlier GitHub-reported snapshot for unpublished local work. Historical
tracking is retired only after the survivor update succeeds. The imported commits remain ordinary
unbookmarked local history.

Broader branch, comment, and stale-record removal remains the job of `cleanup`.

## Tracking-state file format

The file is JSON, validated through typed versioned models. TOML is reserved for human-authored
config. The top-level schema keeps `ReviewIdentity` and `SubmittedBaseline` records separate so an
observation or last-submitted-commit update cannot replace PR identity accidentally.

Reads isolate individual absent, malformed, or obsolete records. One bad identity or baseline is
reported with `relink` or `submit --restart` guidance and does not poison independent useful work.
An unreadable or unsupported top-level file blocks mutation, reports its exact path, and tells the
user how to move it aside before explicitly re-adopting reviews through `checkout` or `relink`.
There is no migration or automatic discard path.

Shape:

```json
{
  "version": 3,
  "review_identities": {
    "<full-change-id>": {
      "version": 2,
      "github_host": "github.com",
      "repository_owner": "octocat",
      "repository_name": "example",
      "pr_number": 123,
      "head_owner": "octocat",
      "head_ref": "review/add-cache-index-ypvmkkuo"
    }
  },
  "stacked_pull_requests": {
    "github.com/octocat/example": true
  },
  "submitted_baselines": {
    "<full-change-id>": {
      "version": 1,
      "commit_id": "0123456789abcdef"
    }
  }
}
```

`ReviewIdentity` records the repository, PR number, and head branch attached to a change;
`SubmittedBaseline` records the exact commit last sent to that PR; `stacked_pull_requests` caches
the one capability fact for a local/GitHub repository pair. PR state, review decisions, URLs,
readiness, native membership, merge results, and cleanup eligibility are observed live. Managed
comments are rediscovered by body marker, and stack topology is derived from the `jj` DAG.

Config goes under `[jj-stack]` in the standard `jj` config scopes
(`jj config edit --user|--repo|--workspace`), for example:

```toml
[jj-stack]
reviewers = ["octocat"]
labels = ["needs-review"]
```

## Current scope

Supported:

- one selected review remote per invocation; resolution prefers `origin` or an unambiguous sole
  remote
- one GitHub repo target
- linear stacks
- visible mutable changes
- one PR per reviewable change

Unsupported:

- merge commits inside the review chain
- divergent changes
- stacked reviews that cross repos or remotes
- review-branch naming collisions caused by generated names
- native stacks of 100 or more reviews; no batching or size-specific recovery is provided

## Bottom line

The central insight is simple:

In a branch-first review tool, stack metadata often becomes part of the core model. In
`jj`, the stack model is already the commit DAG. The tool's job is just to map that DAG
to GitHub's branch-based PR API with stable remote branches.

## References

The design above relies on these upstream `jj` references:

- [glossary](https://docs.jj-vcs.dev/latest/glossary/) for change IDs, rewrites, and visible
  commits
- [bookmarks](https://docs.jj-vcs.dev/latest/bookmarks/) for bookmark behavior, tracking, and
  push safety
- [GitHub workflow](https://docs.jj-vcs.dev/latest/github/) for GitHub integration and `gh`
  caveats
- [configuration](https://docs.jj-vcs.dev/latest/config/) for `jj` configuration
- [templates](https://docs.jj-vcs.dev/latest/templates/) for machine-readable template output
- [FAQ](https://docs.jj-vcs.dev/latest/faq/) for integration guidance
- [technical architecture](https://docs.jj-vcs.dev/latest/technical/architecture/) for why
  `.jj` internals are not an external extension surface
