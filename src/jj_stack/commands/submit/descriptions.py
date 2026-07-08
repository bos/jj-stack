"""Generate pull request and stack descriptions for submit."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError, UsageError
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.stack import LocalRevision

from .models import GeneratedDescription

_DESCRIBE_WITH_STACK_INPUT_ENV = "JJ_STACK_INPUT_FILE"


def resolve_generated_descriptions(
    *,
    descriptions: Sequence[str],
    describe_with: str | None,
    edit: bool = False,
    jj_client: JjClient,
    revisions: tuple[LocalRevision, ...],
    selected_revset: str,
) -> tuple[dict[str, GeneratedDescription], GeneratedDescription | None]:
    """Resolve pull request descriptions and an optional stack description."""

    if descriptions and describe_with is not None:
        raise UsageError(t"Use either {ui.cmd('--describe')} or {ui.cmd('--describe-with')}.")
    if edit and describe_with is not None:
        raise UsageError(t"Use either {ui.cmd('--edit')} or {ui.cmd('--describe-with')}.")

    if describe_with is None:
        default_descriptions = _default_pull_request_descriptions(
            revisions,
            template=_read_pull_request_template(jj_client.repo_root),
        )
        stack_description: GeneratedDescription | None = None
        if descriptions:
            file_descriptions, stack_description = _resolve_description_files(
                descriptions=descriptions,
                jj_client=jj_client,
                revisions=revisions,
            )
            default_descriptions = {
                **default_descriptions,
                **file_descriptions,
            }
        if edit:
            default_descriptions = _edit_descriptions_in_editor(
                descriptions=default_descriptions,
                jj_client=jj_client,
                revisions=revisions,
            )
        return default_descriptions, stack_description

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


def _default_pull_request_descriptions(
    revisions: tuple[LocalRevision, ...],
    *,
    template: str,
) -> dict[str, GeneratedDescription]:
    return {
        revision.change_id: GeneratedDescription(
            body=_pull_request_body(revision.description, template=template),
            title=revision.subject,
        )
        for revision in revisions
    }


_PULL_REQUEST_TEMPLATE_DIRECTORIES = (".github", "", "docs")
_PULL_REQUEST_TEMPLATE_NAMES = ("PULL_REQUEST_TEMPLATE.md", "pull_request_template.md")


def _read_pull_request_template(repo_root: Path) -> str:
    for directory in _PULL_REQUEST_TEMPLATE_DIRECTORIES:
        for name in _PULL_REQUEST_TEMPLATE_NAMES:
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


_EDIT_SEPARATOR_PREFIX = "====== change "
_EDIT_COMMENT_PREFIX = "JJ:"


def render_description_edit_document(
    *,
    descriptions: dict[str, GeneratedDescription],
    revisions: tuple[LocalRevision, ...],
) -> str:
    """Render the `--edit` document, head change first like `view`."""

    lines = [
        "JJ: Edit pull request titles and bodies, then save and close the editor.",
        "JJ: In each change section the first line is the title; the rest is the body.",
        'JJ: Lines starting with "JJ:" are ignored. Do not edit the separator lines.',
    ]
    for revision in reversed(revisions):
        description = descriptions[revision.change_id]
        lines.append("")
        lines.append(f"{_EDIT_SEPARATOR_PREFIX}{revision.change_id}")
        lines.append(description.title)
        if description.body:
            lines.append("")
            lines.extend(description.body.splitlines())
    return "\n".join(lines) + "\n"


def parse_description_edit_document(
    document: str,
    *,
    revisions: tuple[LocalRevision, ...],
) -> dict[str, GeneratedDescription]:
    """Parse an edited `--edit` document, failing closed on anything malformed."""

    known_change_ids = {revision.change_id for revision in revisions}
    sections: dict[str, list[str]] = {}
    current_section: list[str] | None = None
    for line in document.splitlines():
        if line.startswith(_EDIT_COMMENT_PREFIX):
            continue
        if line.startswith(_EDIT_SEPARATOR_PREFIX):
            change_id = line[len(_EDIT_SEPARATOR_PREFIX) :].strip()
            if change_id not in known_change_ids:
                raise CliError(
                    t"Edited pull request descriptions name unknown change "
                    t"{ui.change_id(change_id)}."
                )
            if change_id in sections:
                raise CliError(
                    t"Edited pull request descriptions repeat change "
                    t"{ui.change_id(change_id)}."
                )
            current_section = sections[change_id] = []
            continue
        if current_section is None:
            if line.strip():
                raise CliError(
                    "Edited pull request descriptions have content before the first "
                    "change separator."
                )
            continue
        current_section.append(line)

    parsed: dict[str, GeneratedDescription] = {}
    for revision in revisions:
        section = sections.get(revision.change_id)
        if section is None:
            raise CliError(
                t"Edited pull request descriptions are missing change "
                t"{ui.change_id(revision.change_id)}."
            )
        title_index = 0
        while title_index < len(section) and not section[title_index].strip():
            title_index += 1
        if title_index == len(section):
            raise CliError(
                t"Edited pull request description for "
                t"{ui.change_id(revision.change_id)} has no title line."
            )
        parsed[revision.change_id] = GeneratedDescription(
            body="\n".join(section[title_index + 1 :]).strip(),
            title=section[title_index].strip(),
        )
    return parsed


def _resolve_editor_command(jj_client: JjClient) -> list[str]:
    for candidate in (
        jj_client.get_config_string("ui.editor"),
        os.environ.get("VISUAL"),
        os.environ.get("EDITOR"),
    ):
        if candidate and candidate.strip():
            return _split_editor_command(candidate)
    raise UsageError(
        t"{ui.cmd('--edit')} needs an editor: set jj's {ui.code('ui.editor')} config "
        t"or the {ui.code('VISUAL')} or {ui.code('EDITOR')} environment variable."
    )


def _split_editor_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name != "nt":
        return parts
    return [_strip_surrounding_quotes(part) for part in parts]


def _strip_surrounding_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def _edit_descriptions_in_editor(
    *,
    descriptions: dict[str, GeneratedDescription],
    jj_client: JjClient,
    revisions: tuple[LocalRevision, ...],
) -> dict[str, GeneratedDescription]:
    editor_command = _resolve_editor_command(jj_client)
    document = render_description_edit_document(
        descriptions=descriptions,
        revisions=revisions,
    )
    with tempfile.TemporaryDirectory(prefix="jj-stack-edit-") as tempdir:
        document_path = Path(tempdir) / "pull-request-descriptions.md"
        document_path.write_text(document, encoding="utf-8")
        try:
            completed = subprocess.run(
                [*editor_command, str(document_path)],
                check=False,
                cwd=jj_client.repo_root,
            )
        except FileNotFoundError as error:
            raise CliError(
                t"Editor {ui.cmd(editor_command[0])} was not found."
            ) from error
        except OSError as error:
            raise CliError(
                t"Could not run editor {ui.cmd(editor_command[0])}: {error}"
            ) from error
        if completed.returncode != 0:
            raise CliError(
                t"Editor {ui.cmd(editor_command[0])} exited with status "
                t"{completed.returncode}; submit aborted."
            )
        edited_document = document_path.read_text(encoding="utf-8")
    return parse_description_edit_document(edited_document, revisions=revisions)


def _resolve_description_files(
    *,
    descriptions: Sequence[str],
    jj_client: JjClient,
    revisions: tuple[LocalRevision, ...],
) -> tuple[dict[str, GeneratedDescription], GeneratedDescription | None]:
    generated_descriptions: dict[str, GeneratedDescription] = {}
    generated_stack_description: GeneratedDescription | None = None
    for description in descriptions:
        target, path_text = _parse_description_file_spec(description)
        if target == "stack":
            if len(revisions) <= 1:
                raise UsageError(
                    t"{ui.cmd('--describe stack=FILE')} is only used when the selected "
                    t"stack has more than one change.",
                    hint=t"Use {ui.cmd('--describe CHANGE=FILE')} to set one PR body.",
                )
            if generated_stack_description is not None:
                raise UsageError(t"{ui.cmd('--describe')} specified the stack more than once.")
            generated_stack_description = GeneratedDescription(
                body=_read_description_file(path_text),
                title="",
            )
            continue

        revision = _resolve_description_target(
            jj_client=jj_client,
            revisions=revisions,
            target=target,
        )
        if revision.change_id in generated_descriptions:
            raise UsageError(
                t"{ui.cmd('--describe')} specified {ui.change_id(revision.change_id)} "
                t"more than once."
            )
        generated_descriptions[revision.change_id] = GeneratedDescription(
            body=_read_description_file(path_text),
            title=revision.subject,
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
    revisions: tuple[LocalRevision, ...],
    target: str,
) -> LocalRevision:
    try:
        target_revision = jj_client.resolve_revision(target)
    except CliError as error:
        raise CliError(
            t"Could not resolve {ui.cmd('--describe')} target {ui.revset(target)}: {error}"
        ) from error

    matching_revision = next(
        (
            revision
            for revision in revisions
            if revision.change_id == target_revision.change_id
        ),
        None,
    )
    if matching_revision is None:
        raise UsageError(
            t"{ui.cmd('--describe')} target {ui.revset(target)} is not in the selected stack."
        )
    return matching_revision


def _read_description_file(path_text: str) -> str:
    path = Path(path_text).expanduser()
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except (OSError, UnicodeDecodeError) as error:
        raise CliError(
            t"Could not read description file {ui.cmd(str(path))}: {error}"
        ) from error


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

    return GeneratedDescription(body=body, title=title)


def _pull_request_body(description: str, *, template: str) -> str:
    lines = description.splitlines()
    if not lines:
        return template
    body = "\n".join(lines[1:]).strip()
    if body:
        return body
    if template:
        return template
    return lines[0].strip()
