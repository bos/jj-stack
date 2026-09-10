"""Generate pull request and stack descriptions for submit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError, UsageError
from jj_stack.identifiers import ChangeId
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit

from .default_pr_text import default_pr_body
from .models import GeneratedDescription

_DESCRIBE_WITH_STACK_INPUT_ENV = "JJ_STACK_INPUT_FILE"


def resolve_generated_descriptions(
    *,
    descriptions: Sequence[str],
    describe_with: str | None,
    jj_client: JjClient,
    changes: tuple[LocalCommit, ...],
    selected_revset: str,
    template: str,
) -> tuple[dict[ChangeId, GeneratedDescription], GeneratedDescription | None]:
    """Resolve pull request descriptions and an optional stack description."""

    if descriptions and describe_with is not None:
        raise UsageError(t"Use either {ui.cmd('--describe')} or {ui.cmd('--describe-with')}.")

    if describe_with is None:
        default_descriptions: dict[ChangeId, GeneratedDescription] = {
            change.change_id: GeneratedDescription(
                body=default_pr_body(change.description, template=template),
                title=change.subject,
            )
            for change in changes
        }
        stack_description: GeneratedDescription | None = None
        if descriptions:
            file_descriptions, stack_description = _resolve_description_files(
                descriptions=descriptions,
                jj_client=jj_client,
                changes=changes,
            )
            default_descriptions = {
                **default_descriptions,
                **file_descriptions,
            }
        return default_descriptions, stack_description

    generated_descriptions: dict[ChangeId, GeneratedDescription] = {
        change.change_id: _run_description_command(
            command=describe_with,
            kind="pr",
            repo_root=jj_client.repo_root,
            revset=change.change_id,
        )
        for change in changes
    }
    generated_stack_description = None
    if len(changes) > 1:
        stack_input = _build_stack_description_input(
            generated_descriptions=generated_descriptions,
            jj_client=jj_client,
            changes=changes,
        )
        with tempfile.TemporaryDirectory(prefix="jj-stack-describe-with-") as tempdir:
            stack_input_path = Path(tempdir) / "stack-input.json"
            stack_input_path.write_text(json.dumps(stack_input), encoding="utf-8")
            generated_stack_description = _run_description_command(
                command=describe_with,
                extra_env={
                    _DESCRIBE_WITH_STACK_INPUT_ENV: str(stack_input_path),
                },
                kind="stack",
                repo_root=jj_client.repo_root,
                revset=selected_revset,
            )
    return generated_descriptions, generated_stack_description


def preserve_external_pr_text(
    *,
    descriptions: dict[ChangeId, GeneratedDescription],
    prs: Mapping[ChangeId, GithubPR | None],
    submitted_commits: dict[ChangeId, LocalCommit],
    template: str,
) -> dict[ChangeId, GeneratedDescription]:
    """Preserve a live PR pair unless its text still matches the submitted description."""

    preserved: dict[ChangeId, GeneratedDescription] = {}
    for change_id, description in descriptions.items():
        pr = prs[change_id]
        submitted = submitted_commits.get(change_id)
        if pr is None:
            preserved[change_id] = description
            continue

        live_body = pr.body or ""
        follows_submitted_description = submitted is not None and (
            pr.title == submitted.subject
            and live_body == default_pr_body(submitted.description, template=template)
        )
        preserve_existing = not follows_submitted_description
        preserved[change_id] = replace(
            description,
            body=(
                live_body
                if preserve_existing and "body" not in description.explicit_fields
                else description.body
            ),
            title=(
                pr.title
                if preserve_existing and "title" not in description.explicit_fields
                else description.title
            ),
        )
    return preserved


_PR_TEMPLATE_DIRECTORIES = (".github", "", "docs")
_PR_TEMPLATE_NAMES = ("PULL_REQUEST_TEMPLATE.md", "pull_request_template.md")


def read_pr_template(repo_root: Path) -> str:
    for directory in _PR_TEMPLATE_DIRECTORIES:
        for name in _PR_TEMPLATE_NAMES:
            path = repo_root / directory / name
            if not path.is_file():
                continue
            try:
                return path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as error:
                raise CliError(
                    t"Could not read pull request template {ui.cmd(str(path))}: {error}"
                ) from error
    return ""


def _resolve_description_files(
    *,
    descriptions: Sequence[str],
    jj_client: JjClient,
    changes: tuple[LocalCommit, ...],
) -> tuple[dict[ChangeId, GeneratedDescription], GeneratedDescription | None]:
    generated_descriptions: dict[ChangeId, GeneratedDescription] = {}
    generated_stack_description: GeneratedDescription | None = None
    for description in descriptions:
        target, path_text = _parse_description_file_spec(description)
        if target == "stack":
            if len(changes) <= 1:
                raise UsageError(
                    t"{ui.cmd('--describe stack=FILE')} is only used when the selected "
                    t"stack has more than one change.",
                    hint=t"Use {ui.cmd('--describe CHANGE=FILE')} to set one PR body.",
                )
            if generated_stack_description is not None:
                raise UsageError(t"{ui.cmd('--describe')} specified the stack more than once.")
            generated_stack_description = GeneratedDescription(
                body=_read_description_file(path_text),
                explicit_fields=frozenset(("body",)),
                title="",
            )
            continue

        change = _resolve_description_target(
            jj_client=jj_client,
            changes=changes,
            target=target,
        )
        if change.change_id in generated_descriptions:
            raise UsageError(
                t"{ui.cmd('--describe')} specified {ui.change_id(change.change_id)} "
                t"more than once."
            )
        generated_descriptions[change.change_id] = GeneratedDescription(
            body=_read_description_file(path_text),
            explicit_fields=frozenset(("body",)),
            title=change.subject,
        )
    return generated_descriptions, generated_stack_description


def _parse_description_file_spec(description: str) -> tuple[str, str]:
    target, separator, path_text = description.partition("=")
    target = target.strip()
    path_text = path_text.strip()
    if not separator or not target or not path_text:
        raise UsageError(
            t"Expected {ui.cmd('--describe')} value in the form "
            t"{ui.cmd('CHANGE=FILE')} or {ui.cmd('stack=FILE')}."
        )
    return target, path_text


def _resolve_description_target(
    *,
    jj_client: JjClient,
    changes: tuple[LocalCommit, ...],
    target: str,
) -> LocalCommit:
    try:
        target_change = jj_client.resolve_commit(target)
    except CliError as error:
        raise CliError(
            t"Could not resolve {ui.cmd('--describe')} target {ui.revset(target)}: {error}"
        ) from error

    matching_change = next(
        (change for change in changes if change.change_id == target_change.change_id),
        None,
    )
    if matching_change is None:
        raise UsageError(
            t"{ui.cmd('--describe')} target {ui.revset(target)} is not in the selected stack."
        )
    return matching_change


def _read_description_file(path_text: str) -> str:
    path = Path(path_text).expanduser()
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except (OSError, UnicodeDecodeError) as error:
        raise CliError(t"Could not read description file {ui.cmd(str(path))}: {error}") from error


def _build_stack_description_input(
    *,
    generated_descriptions: dict[ChangeId, GeneratedDescription],
    jj_client: JjClient,
    changes: tuple[LocalCommit, ...],
) -> dict[str, object]:
    try:
        diffstats = jj_client.diffstats(tuple(change.commit_id for change in changes))
    except JjCommandError as error:
        raise CliError(t"Could not collect diffstats for the selected stack: {error}") from error
    return {
        "changes": [
            {
                "body": generated_descriptions[change.change_id].body,
                "change_id": change.change_id,
                "diffstat": diffstats[change.commit_id],
                "title": generated_descriptions[change.change_id].title,
            }
            for change in changes
        ]
    }


def _run_description_command(
    *,
    command: str,
    extra_env: dict[str, str] | None = None,
    kind: Literal["pr", "stack"],
    repo_root: Path,
    revset: str,
) -> GeneratedDescription:
    command_args = [command, f"--{kind}", revset]
    if os.name == "nt" and Path(command).suffix.lower() == ".py":
        command_args = [sys.executable, command, f"--{kind}", revset]
    try:
        completed = subprocess.run(
            command_args,
            capture_output=True,
            check=False,
            cwd=repo_root,
            env=(
                None
                if extra_env is None
                else {
                    **os.environ,
                    **extra_env,
                }
            ),
            text=True,
        )
    except FileNotFoundError as error:
        raise CliError(t"Describe helper {ui.cmd(command)} was not found.") from error
    except OSError as error:
        raise CliError(t"Could not run describe helper {ui.cmd(command)}: {error}") from error

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if not detail:
            detail = f"exit status {completed.returncode}"
        raise CliError(
            t"Describe helper {ui.cmd(command)} failed for {ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}: {detail}"
        )

    output = completed.stdout.strip()
    if not output:
        raise CliError(
            t"Describe helper {ui.cmd(command)} produced no JSON for "
            t"{ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}."
        )

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise CliError(
            t"Describe helper {ui.cmd(command)} returned invalid JSON for "
            t"{ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise CliError(
            t"Describe helper {ui.cmd(command)} must return a JSON object for "
            t"{ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}."
        )

    title = payload.get("title")
    body = payload.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        raise CliError(
            t"Describe helper {ui.cmd(command)} must return string "
            t"{ui.cmd('title')} and "
            t"{ui.cmd('body')} fields for "
            t"{ui.cmd(f'--{kind}')} {ui.revset(revset)}."
        )

    return GeneratedDescription(
        body=body,
        explicit_fields=frozenset(("body", "title")),
        title=title,
    )
