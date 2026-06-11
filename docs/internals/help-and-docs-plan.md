# User-Facing Help and Docs Plan

This file is the working plan for improving `jj-review`'s user-facing help and docs.

The goal is a docs/help surface that fits this tool's actual model, teaches the right mental
model to jj users, and makes recovery paths easy to find. Competitor parity is not the goal.

When this plan conflicts with another internal doc, treat them this way:

1. `docs/AGENTS.md` sets the standard for user-facing language.
2. `docs/internals/design.md` is the current product spec, but it is not above criticism.
3. `docs/internals/implementation-strategy.md` records how the current surface was built.
4. this file focuses on whether that surface makes sense to users.

If a user-facing docs/help improvement reveals that the current design is confusing, too
internal, or otherwise not user-focused enough, update the design first rather than forcing the
docs to preserve a bad explanation.

Once a slice here ships, remove it from this file and note the result in
`implementation-strategy.md` if it changed user-visible behavior.

## 1. What is already good

These are strengths worth preserving rather than replacing:

- top-level help is already curated instead of being a flat parser dump
- hidden repair commands stay out of default help
- command help already uses real command descriptions rather than empty boilerplate
- docs already separate user-facing material under `docs/` from contributor notes under
  `docs/internals/`
- `troubleshooting.md` is already organized by symptom and next command
- the product already has strong repair and diagnostic commands such as `doctor`,
  `checkout`, `relink`, and `cleanup --rebase`

The biggest remaining gap is not "missing feature parity". It is that some important user
questions still require reading several pages or inferring behavior from command output.

## 2. Principles

1. Teach `jj-review`, not competitor workflows.
2. Assume the reader already knows `jj` and `git`.
3. Keep user docs and `--help` free of internal design-doc jargon.
4. Describe what the user should do next, not how the implementation feels about itself.
5. Prefer concrete examples and recovery recipes over exhaustive taxonomy.
6. Avoid large information-architecture churn unless it clearly improves findability.
7. Keep advanced repair commands advanced.
8. When the current design and a user-comprehensible explanation conflict, fix the design rather
   than teaching a worse model.

## 3. What to improve in `--help`

### 3a. Keep the current help shape

Do not replace the current help system with a rigid imported template.

The current shape is already sensible:

- `Usage`
- command description
- `Positional Arguments` when needed
- `Options`

Improve within that shape instead of forcing every command into a six-section template.

### 3b. Add examples where they materially help

Highest-value commands should grow 2-3 short examples in long help:

- `submit`
- `view`
- `land`
- `unstack`
- `checkout`
- `cleanup --rebase`

Examples should come from real workflows already documented in `docs/`, not invented toy
cases. The target is fast recognition:

- normal path
- common recovery path
- one selector example where the command supports explicit targeting

### 3c. Tighten command descriptions

Each command description should answer the questions a user has at the terminal:

- what state does this inspect or mutate?
- what does it act on by default?
- what important safety boundary should the user know?

For commands that need GitHub, it is reasonable to say so plainly in the description. Keep it
short and factual.

### 3d. Small consistency checks are worthwhile

A lightweight help-text test is worth owning, but it should validate useful consistency rather
than a brittle imported style guide.

Good candidates:

- top-level help still hides advanced repair commands by default
- `help --all` still exposes the full command surface
- each command has non-empty summary/help text
- descriptions do not regress into banned internal jargon from `docs/AGENTS.md`

Avoid tests that require every command to use the exact same prose structure.

## 4. What to improve in `docs/`

### 4a. Keep the current split, but fill obvious gaps

The current set:

- `docs/README.md`
- `docs/mental-model.md`
- `docs/daily-workflow.md`
- `docs/troubleshooting.md`

is already small and understandable. Keep that unless growth makes a split clearly better.

The work should focus on content gaps first:

- missing recovery scenarios
- missing explanation of jj↔GitHub round trips
- better examples of stack lifecycle after land / restack / checkout

### 4b. Add one focused workflow page

The strongest missing page is a focused "respond to review" or "revise and resubmit" guide.

That page should cover:

- amend vs. rebase from the user's point of view
- when `submit` is enough and when `cleanup --rebase` is the right next step
- what happens after part of a stack lands
- what to do when review state exists on GitHub but not in the current workspace

This is more valuable than a large workflow directory.

### 4c. Add one reusable stack diagram

An ASCII diagram is worthwhile if it teaches the right model:

```text
@
│
○ change_id: xyz   review/top-xyz      PR #3
│
○ change_id: abc   review/middle-abc   PR #2
│
○ change_id: qrs   review/bottom-qrs   PR #1
│
◉ trunk()
```

Rules for the diagram:

- identify revisions by `change_id`
- show review bookmarks as `review/...` or another explicitly configured review prefix
- do not teach user feature branches as if they were `jj-review`'s managed transport branches
- reuse the same diagram shape where it helps, rather than creating competing diagrams

### 4d. A glossary is optional, not mandatory

Do not create a glossary just to centralize internal jargon.

If a glossary is added later, it should define only real user-facing terms that already appear
in docs, such as:

- change ID
- review bookmark
- stack
- trunk

Do not use it as permission to reintroduce banned phrases like `ready prefix`.

## 5. Troubleshooting gaps to fill

`troubleshooting.md` is already the highest-value page in the tree. The next expansion should
cover the cases users are likely to hit in real repos:

- a bottom PR was squash-merged on GitHub and remaining changes now need local repair
- a middle PR was closed or otherwise stopped being part of the live stack
- a user pushed to a managed review bookmark manually and later `view` or `submit` fails
  closed
- `trunk()` advanced, but nothing landed, so the right answer is plain `jj rebase` rather than
  `cleanup --rebase`
- `cleanup --rebase` encounters conflicts and the user needs to finish the rewrite in `jj`
- review state exists on another machine or workspace and needs `checkout`

Each entry should stay in the existing pattern:

- symptom
- likely cause
- next command or repair path

## 6. Things this plan should explicitly not do

These ideas are out of scope unless the product direction changes:

- do not use competitor parity as the success metric
- do not promote `unlink` into the default help surface; it is an advanced repair command
- do not add a `jj-review docs` command
- do not introduce a semantic exit-code taxonomy just for documentation symmetry
- do not force a major docs tree rewrite before the content gaps are fixed
- do not add user-facing jargon that `docs/AGENTS.md` already bans
- do not teach `feat/*` or other ordinary feature branches as the review branches
  `jj-review` manages

## 7. Sequencing

Priority order:

1. Add `--help` examples for the main lifecycle and recovery commands.
2. Expand `troubleshooting.md` with the missing real-world failure cases.
3. Add one focused page for "respond to review" / revise-resubmit workflows.
4. Add one reusable ASCII stack diagram and place it where it clarifies the model.
5. Add small consistency tests or lint checks for help/docs vocabulary.
6. Re-evaluate whether the docs tree still needs structural changes after the content work.

## 8. Done means

This plan is succeeding when a user can quickly answer questions like:

- "What stack will `submit` operate on if I run it right now?"
- "Why did `land` stop, and what do I run next?"
- "Part of my stack landed; how do I fix the rest locally?"
- "These PRs exist on GitHub already; how do I reconnect this workspace?"
- "What are those `review/...` bookmarks and when should I care about them?"

If the docs/help changes do not make those answers easier to find, they are probably churn.
