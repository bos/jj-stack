"""Generate pull request and stack descriptions for submit."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.stack import LocalRevision

from .models import GeneratedDescription

_DESCRIBE_WITH_STACK_INPUT_ENV = "JJ_REVIEW_STACK_INPUT_FILE"


def resolve_generated_descriptions(
    *,
    describe_with: str | None,
    jj_client: JjClient,
    revisions: tuple[LocalRevision, ...],
    selected_revset: str,
) -> tuple[dict[str, GeneratedDescription], GeneratedDescription | None]:
    """Resolve pull request descriptions and an optional stack description."""

    if describe_with is None:
        return (
            {
                revision.change_id: GeneratedDescription(
                    body=_pull_request_body(revision.description),
                    title=revision.subject,
                )
                for revision in revisions
            },
            None,
        )

    generated_descriptions = {
        revision.change_id: _run_description_command(
            command=describe_with,
            kind="pr",
            repo_root=jj_client.repo_root,
            revset=revision.change_id,
        )
        for revision in revisions
    }
    generated_stack_description = None
    if len(revisions) > 1:
        stack_input = _build_stack_description_input(
            generated_descriptions=generated_descriptions,
            jj_client=jj_client,
            revisions=revisions,
        )
        with tempfile.TemporaryDirectory(prefix="jj-review-describe-with-") as tempdir:
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


def _build_stack_description_input(
    *,
    generated_descriptions: dict[str, GeneratedDescription],
    jj_client: JjClient,
    revisions: tuple[LocalRevision, ...],
) -> dict[str, object]:
    diffstats = _describe_with_diffstats(jj_client=jj_client, revisions=revisions)
    return {
        "revisions": [
            {
                "body": generated_descriptions[revision.change_id].body,
                "change_id": revision.change_id,
                "diffstat": diffstats[revision.change_id],
                "title": generated_descriptions[revision.change_id].title,
            }
            for revision in revisions
        ]
    }


def _describe_with_diffstats(
    *,
    jj_client: JjClient,
    revisions: tuple[LocalRevision, ...],
) -> dict[str, str]:
    if not revisions:
        return {}
    if len(revisions) == 1:
        revision = revisions[0]
        return {
            revision.change_id: _describe_with_diffstat(
                jj_client=jj_client,
                revset=revision.change_id,
            )
        }

    def describe_revision(revision: LocalRevision) -> tuple[str, str]:
        return (
            revision.change_id,
            _describe_with_diffstat(
                jj_client=jj_client,
                revset=revision.change_id,
            ),
        )

    with ThreadPoolExecutor(max_workers=min(len(revisions), 10)) as pool:
        return dict(pool.map(describe_revision, revisions))


def _describe_with_diffstat(*, jj_client: JjClient, revset: str) -> str:
    try:
        stdout = jj_client.show_with_stat(revset)
    except JjCommandError as error:
        raise CliError(
            t"Could not collect diffstat for --stack {ui.revset(revset)}: {error}"
        ) from error

    lines = stdout.rstrip().splitlines()
    diffstat_lines: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            if diffstat_lines:
                break
            continue
        diffstat_lines.append(line)
    return "\n".join(reversed(diffstat_lines))


def _run_description_command(
    *,
    command: str,
    extra_env: dict[str, str] | None = None,
    kind: Literal["pr", "stack"],
    repo_root: Path,
    revset: str,
) -> GeneratedDescription:
    try:
        completed = subprocess.run(
            [command, f"--{kind}", revset],
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

    return GeneratedDescription(body=body, title=title)


def _pull_request_body(description: str) -> str:
    lines = description.splitlines()
    if not lines:
        return ""
    body = "\n".join(lines[1:]).strip()
    if body:
        return body
    return lines[0].strip()
