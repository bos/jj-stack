from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalRevision
from jj_stack.review.github_stack_sync import (
    GithubStackSurvivorReview,
    build_selected_github_stack_sync,
)
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review.repository import observe_repository_paths
from jj_stack.review.status import PreparedStatus
from jj_stack.review.trunk_evidence import (
    TrackedReview,
    TrunkEvidenceKind,
    proven_kind,
)
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class OnTrunkChange:
    candidate: TrackedReview
    evidence_kind: TrunkEvidenceKind
    requires_terminal_pull_request: bool
    revision: LocalRevision | None


@dataclass(frozen=True, slots=True)
class SelectedConvergencePlan:
    on_trunk: tuple[OnTrunkChange, ...]
    github_stack_survivors: tuple[GithubStackSurvivorReview, ...]
    reviewed_survivors: tuple[LocalRevision, ...]
    survivors: tuple[LocalRevision, ...]


def build_selected_convergence_plan(
    *,
    context: CommandContext,
    github_stacks: tuple[GithubStack, ...],
    observation: RepositoryObservation,
    prepared_status: PreparedStatus,
    repository: GithubRepoAddress,
) -> SelectedConvergencePlan:
    selected = prepared_status.prepared.stack.revisions
    state = prepared_status.prepared.state
    stack_history, stack_survivor_reviews = build_selected_github_stack_sync(
        context=context,
        github_stacks=github_stacks,
        observation=observation,
        repository=repository,
        selected=selected,
        state=state,
        trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
    )
    stack_history_by_change_id = {item.candidate.change_id: item for item in stack_history}
    github_stack_survivors = {item.candidate.change_id: item for item in stack_survivor_reviews}
    on_trunk: list[OnTrunkChange] = [
        OnTrunkChange(
            candidate=item.candidate,
            evidence_kind=item.evidence_kind,
            requires_terminal_pull_request=True,
            revision=item.revision,
        )
        for item in stack_history_by_change_id.values()
    ]
    survivors: list[LocalRevision] = []
    for revision in filter(
        lambda item: item.change_id not in stack_history_by_change_id,
        selected,
    ):
        candidate = state.tracked_review(revision.change_id)
        evidence_kind = (
            None
            if candidate is None or revision.change_id in github_stack_survivors
            else _trunk_evidence_kind_for(
                candidate=candidate,
                context=context,
                observation=observation,
                repository=repository,
                trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
            )
        )
        if candidate is None or evidence_kind is None:
            survivors.append(revision)
            continue
        if survivors:
            raise CliError(
                t"Cannot sync reviewed {ui.change_id(revision.change_id)} because these "
                t"unmerged local changes are its parents: "
                t"{ui.join(lambda item: ui.change_id(item.change_id), tuple(survivors))}. "
                t"The submitted review is already on fetched trunk, so sync cannot decide "
                t"whether those local changes belong before or after it.\n"
                t"Submitted commit: "
                t"{ui.semantic_text(candidate.submitted_baseline.commit_id, 'commit_id')}\n"
                t"Local copy commit: {ui.semantic_text(revision.commit_id, 'commit_id')}\n"
                t"Fetched trunk commit: "
                t"{
                    ui.semantic_text(prepared_status.prepared.stack.trunk.commit_id, 'commit_id')
                }",
                hint=t"Inspect the local and fetched histories with "
                t"{
                    ui.cmd(f"jj log -r 'trunk() | (trunk()..{selected[-1].commit_id})'")
                }. Choose the intended order with {ui.cmd('jj')}; ask an agent to inspect "
                t"this repository and these commit IDs if useful. Then inspect the remaining "
                t"local reviews with {ui.cmd('jj-stack view')}. Run "
                t"{ui.cmd('jj-stack sync <head-change-id>')} for a remaining mutable reviewed "
                t"head, or {ui.cmd('jj-stack cleanup')} if none remains.",
            )
        on_trunk.append(
            OnTrunkChange(
                candidate=candidate,
                evidence_kind=evidence_kind,
                requires_terminal_pull_request=False,
                revision=revision,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk))
    _require_no_checked_out_merged_changes(tuple(on_trunk), context=context)
    reviewed: list[LocalRevision] = []
    saw_unreviewed = False
    for revision in survivors:
        candidate = state.tracked_review(revision.change_id)
        if candidate is None:
            saw_unreviewed = True
            continue
        if saw_unreviewed:
            raise CliError(
                t"Cannot sync because reviewed {ui.change_id(revision.change_id)} appears "
                t"above an unreviewed change.",
                hint="Submit the intervening change or select a stack that ends below it.",
            )
        pull_request = observation.reviews[revision.change_id].pull_request
        identity = candidate.review_identity
        if (
            pull_request is None
            or identity.repository_key != repository.repository_key
            or not identity.matches_pull_request(pull_request)
        ):
            raise CliError(
                t"The pull request no longer matches saved tracking for "
                t"{ui.change_id(candidate.change_id)}.",
                hint=t"Reattach the intended review with {ui.cmd('jj-stack relink')}, or forget "
                t"the incorrect link with {ui.cmd('jj-stack unstack --local')} before "
                t"submitting again.",
            )
        lifecycle = pull_request.normalize_state().state
        if lifecycle != "open":
            raise CliError(
                t"PR #{pull_request.number} for {ui.change_id(candidate.change_id)} is "
                t"{lifecycle}, so sync cannot update that review.",
                hint=t"Reopen it on GitHub, or run {ui.cmd('jj-stack cleanup')} before "
                t"submitting again.",
            )
        reviewed.append(revision)
    plan = SelectedConvergencePlan(
        on_trunk=tuple(on_trunk),
        github_stack_survivors=tuple(github_stack_survivors.values()),
        reviewed_survivors=tuple(reviewed),
        survivors=tuple(survivors),
    )
    _require_no_divergent_survivors(plan)
    return plan


def _trunk_evidence_kind_for(
    *,
    candidate: TrackedReview,
    context: CommandContext,
    observation: RepositoryObservation,
    repository: GithubRepoAddress,
    trunk_commit_id: str,
) -> TrunkEvidenceKind | None:
    observed = observation.reviews[candidate.change_id]
    if observed.identity != candidate.review_identity:
        raise CliError(
            t"Saved PR tracking changed for {ui.change_id(candidate.change_id)}.",
            hint=t"Inspect it with {ui.cmd('jj-stack view')}, then reattach the intended "
            t"review with {ui.cmd('jj-stack relink')}.",
        )
    pull_request = observed.pull_request
    if pull_request is None:
        raise CliError(
            t"GitHub no longer reports PR #{candidate.review_identity.pr_number}.",
            hint=t"Confirm it with {ui.cmd('jj-stack view')}, then reattach an open "
            t"replacement with {ui.cmd('jj-stack relink')}, or forget the missing link with "
            t"{ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    evidence_kind, reason = proven_kind(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=repository,
        trunk_commit_id=trunk_commit_id,
    )
    if evidence_kind is None and pull_request.normalize_state().state in {"closed", "merged"}:
        raise CliError(
            t"Cannot remove {ui.change_id(candidate.change_id)}: {reason}.",
            hint="Make GitHub's reported merge commit reachable from trunk, then rerun sync.",
        )
    return evidence_kind


def _require_no_divergent_survivors(plan: SelectedConvergencePlan) -> None:
    for revision in plan.survivors:
        if revision.divergent:
            raise CliError(
                t"Cannot rebase remaining {ui.change_id(revision.change_id)} because it has "
                t"multiple visible revisions.",
                hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this "
                t"stack.",
            )


def rewritten_removal_blocker(
    *,
    candidate: TrackedReview,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> Message | None:
    change = next(item for item in plan.on_trunk if item.candidate == candidate)
    if change.evidence_kind == "exact":
        return None
    ancestor_commit_id = (
        change.revision.commit_id
        if change.revision is not None
        else change.candidate.submitted_baseline.commit_id
    )
    recovery = dependent_path_commands(
        ancestor_commit_id=ancestor_commit_id,
        context=context,
        excluded_change_ids={
            *(item.candidate.change_id for item in plan.on_trunk),
            *(revision.change_id for revision in plan.survivors),
        },
    )
    return (
        None
        if recovery is None
        else t"another local stack still uses this merged change; {recovery}"
    )


def _require_no_unpublished_edits(
    changes: tuple[OnTrunkChange, ...],
) -> None:
    for item in changes:
        if item.revision is not None and item.revision.holds_unpublished_edit(
            (item.candidate.submitted_baseline.commit_id,)
        ):
            raise CliError(
                t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because it has "
                t"unpublished local edits since submit.",
                hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun "
                t"sync.",
            )


def _require_no_checked_out_merged_changes(
    changes: tuple[OnTrunkChange, ...],
    *,
    context: CommandContext,
) -> None:
    for item in changes:
        revision = item.revision
        if revision is None or not revision.is_working_copy:
            continue
        workspaces = revision.working_copy_workspaces
        if not workspaces:
            location = "the current workspace"
        elif len(workspaces) == 1:
            location = t"workspace {ui.code(workspaces[0])}"
        else:
            location = t"workspaces {ui.join(ui.code, workspaces)}"
        raise CliError(
            t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because it is "
            t"checked out in {location}.",
            hint=_checked_out_workspace_hint(workspaces=workspaces, context=context),
        )


def _checked_out_workspace_hint(
    *,
    workspaces: tuple[str, ...],
    context: CommandContext,
) -> Message:
    known = {workspace.name: workspace for workspace in context.jj_client.list_workspaces()}
    if not workspaces:
        workspaces = tuple(workspace.name for workspace in known.values() if workspace.current)
    hint: list[Message] = ["Move off the merged change in each workspace:\n"]
    disposable: list[tuple[str, str]] = []
    for name in workspaces:
        workspace = known.get(name)
        if workspace is None or (workspace.root is None and not workspace.current):
            hint.append(
                t"For {ui.code(name)}, run {ui.cmd("jj new 'trunk()'")} in that workspace.\n"
            )
            continue
        root = str(workspace.root or context.repo_root)
        command = _workspace_move_command(root=root, platform=sys.platform)
        shell = " (PowerShell)" if sys.platform == "win32" else ""
        hint.append(t"For {ui.code(name)} at {ui.code(root)}{shell}:\n  {ui.cmd(command)}\n")
        if not workspace.current:
            disposable.append((name, root))

    if disposable:
        hint.append(
            "Alternatively, forget and move to the trash any workspace that is no longer "
            "needed:\n"
        )
        for name, root in disposable:
            command = _workspace_disposal_command(name=name, root=root, platform=sys.platform)
            shell = " (PowerShell)" if sys.platform == "win32" else ""
            hint.append(t"For {ui.code(name)}{shell}:\n  {ui.cmd(command)}\n")
    hint.append("Then rerun the same sync command.")
    return tuple(hint)


def _workspace_move_command(*, root: str, platform: str) -> str:
    if platform == "win32":
        return (
            f"Push-Location -LiteralPath {_powershell_quote(root)}; try {{ "
            "jj new 'trunk()' } finally { Pop-Location }"
        )
    return f"(cd {shlex.quote(root)} && jj new {shlex.quote('trunk()')})"


def _workspace_disposal_command(*, name: str, root: str, platform: str) -> str:
    if platform == "win32":
        quoted_name = _powershell_quote(name)
        quoted_root = _powershell_quote(root)
        return (
            f"jj workspace forget -- {quoted_name}; if ($LASTEXITCODE -eq 0) {{ "
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
            f"{quoted_root}, "
            "[Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
            "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin) }"
        )
    trash_command = "trash" if platform == "darwin" else "gio trash"
    return f"(jj workspace forget -- {shlex.quote(name)} && {trash_command} {shlex.quote(root)})"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def dependent_path_commands(
    *,
    ancestor_commit_id: str,
    context: CommandContext,
    excluded_change_ids: set[str] | None = None,
) -> Message | None:
    heads = dependent_path_heads(
        ancestor_commit_id=ancestor_commit_id,
        context=context,
        excluded_change_ids=excluded_change_ids,
    )
    if not heads:
        return None
    return t"run {ui.join(lambda r: ui.cmd(f'jj-stack sync {r.change_id[:8]}'), heads)}"


def dependent_path_heads(
    *,
    ancestor_commit_id: str,
    context: CommandContext,
    excluded_change_ids: set[str] | None = None,
) -> tuple[LocalRevision, ...]:
    excluded_changes = excluded_change_ids or set()
    repository_paths = observe_repository_paths(
        jj_client=context.jj_client,
        descendant_of=(ancestor_commit_id,),
        include_working_copies=True,
        state=context.state_store.load(),
    )
    heads_by_commit_id: dict[str, LocalRevision] = {}
    for path in repository_paths.paths:
        head = next(
            (
                revision
                for revision in reversed(path.stack.revisions)
                if revision.change_id not in excluded_changes
            ),
            None,
        )
        if head is not None:
            heads_by_commit_id[head.commit_id] = head
    return tuple(heads_by_commit_id.values())
