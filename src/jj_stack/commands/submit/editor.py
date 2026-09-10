"""Edit pull request titles, bodies, and draft state in the user's editor before submit."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import tomllib
from pathlib import Path

import jj_stack.ui as ui
from jj_stack.errors import CliError, UsageError
from jj_stack.identifiers import ChangeId
from jj_stack.jj.client import JjClient
from jj_stack.models.stack import LocalCommit

from .models import GeneratedDescription

_EDIT_SEPARATOR_PREFIX = "====== change "
_EDIT_COMMENT_PREFIX = "JJ:"
_EDIT_DRAFT_PREFIX = "JJ: Draft:"


def render_description_edit_document(
    *,
    descriptions: dict[ChangeId, GeneratedDescription],
    drafts: dict[ChangeId, bool],
    changes: tuple[LocalCommit, ...],
) -> str:
    """Render the `--edit` document, head change first like `view`."""

    lines = [
        "JJ: Edit pull request titles, bodies, and draft state, then save and close.",
        "JJ: Change Draft to yes or no. The short forms y and n are also accepted.",
        "JJ: In each change section the first line is the title; the rest is the body.",
        'JJ: Other lines starting with "JJ:" are ignored. Do not edit the separators.',
    ]
    for change in reversed(changes):
        description = descriptions[change.change_id]
        lines.append("")
        lines.append(f"{_EDIT_SEPARATOR_PREFIX}{change.change_id}")
        lines.append(f"{_EDIT_DRAFT_PREFIX} {'yes' if drafts[change.change_id] else 'no'}")
        lines.append(description.title)
        if description.body:
            lines.append("")
            lines.extend(description.body.splitlines())
    return "\n".join(lines) + "\n"


def parse_description_edit_document(
    document: str,
    *,
    changes: tuple[LocalCommit, ...],
) -> tuple[dict[ChangeId, GeneratedDescription], dict[ChangeId, bool]]:
    """Parse an edited `--edit` document, failing closed on anything malformed."""

    known_change_ids = {change.change_id for change in changes}
    sections: dict[ChangeId, list[str]] = {}
    drafts: dict[ChangeId, bool] = {}
    current_change_id: ChangeId | None = None
    current_section: list[str] | None = None
    for line in document.splitlines():
        if line.startswith(_EDIT_SEPARATOR_PREFIX):
            change_id = ChangeId(line[len(_EDIT_SEPARATOR_PREFIX) :].strip())
            if change_id not in known_change_ids:
                raise CliError(
                    t"Edited pull request descriptions name unknown change "
                    t"{ui.change_id(change_id)}."
                )
            if change_id in sections:
                raise CliError(
                    t"Edited pull request descriptions repeat change {ui.change_id(change_id)}."
                )
            current_change_id = change_id
            current_section = sections[change_id] = []
            continue
        if line.startswith(_EDIT_DRAFT_PREFIX):
            if current_change_id is None:
                raise CliError("Edited draft state appears before the first change separator.")
            if current_change_id in drafts:
                raise CliError(
                    t"Edited pull request repeats draft state for "
                    t"{ui.change_id(current_change_id)}."
                )
            value = line[len(_EDIT_DRAFT_PREFIX) :].strip()
            normalized = value.lower()
            if normalized not in {"yes", "y", "no", "n"}:
                raise CliError(
                    t"Edited pull request draft state for "
                    t"{ui.change_id(current_change_id)} is {ui.code(value or '(empty)')}; "
                    t"expected yes or no (y or n also work)."
                )
            drafts[current_change_id] = normalized in {"yes", "y"}
            continue
        if line.startswith(_EDIT_COMMENT_PREFIX):
            continue
        if current_section is None:
            if line.strip():
                raise CliError(
                    "Edited pull request descriptions have content before the first "
                    "change separator."
                )
            continue
        current_section.append(line)

    parsed: dict[ChangeId, GeneratedDescription] = {}
    for change in changes:
        section = sections.get(change.change_id)
        if section is None:
            raise CliError(
                t"Edited pull request descriptions are missing change "
                t"{ui.change_id(change.change_id)}."
            )
        if change.change_id not in drafts:
            raise CliError(
                t"Edited pull request is missing draft state for "
                t"{ui.change_id(change.change_id)}."
            )
        title_index = 0
        while title_index < len(section) and not section[title_index].strip():
            title_index += 1
        if title_index == len(section):
            raise CliError(
                t"Edited pull request description for "
                t"{ui.change_id(change.change_id)} has no title line."
            )
        parsed[change.change_id] = GeneratedDescription(
            body="\n".join(section[title_index + 1 :]).strip(),
            title=section[title_index].strip(),
        )
    return parsed, drafts


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
    argv = _editor_command_from_toml_array(command)
    if argv is not None:
        return argv
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name != "nt":
        return parts
    return [_strip_surrounding_quotes(part) for part in parts]


def _editor_command_from_toml_array(command: str) -> list[str] | None:
    """Return the argv for a list-valued `ui.editor`, or None when it is a plain string.

    `jj` accepts either form and `jj config get` prints a list back as its TOML text, so
    splitting that text as a shell word would look for an editor named `[code,--wait]`.
    """

    if not command.startswith("["):
        return None
    try:
        parsed = tomllib.loads(f"editor = {command}")
    except tomllib.TOMLDecodeError:
        return None
    value = parsed["editor"]
    if not isinstance(value, list):
        return None
    argv: list[str] = []
    for part in value:
        if not isinstance(part, str):
            return None
        if part:
            argv.append(part)
    return argv or None


def _strip_surrounding_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def resume_edit_hint(document_path: Path) -> ui.Message:
    quoted_document_path = (
        subprocess.list2cmdline([str(document_path)])
        if os.name == "nt"
        else shlex.quote(str(document_path))
    )
    retry = f"--resume-edit {quoted_document_path}"
    return (
        t"Retry the same {ui.cmd('jj-stack submit')} command with {ui.cmd(retry)} "
        t"instead of {ui.option('--edit')}, keeping the other options."
    )


def edit_prs_in_editor(
    *,
    descriptions: dict[ChangeId, GeneratedDescription],
    drafts: dict[ChangeId, bool],
    jj_client: JjClient,
    changes: tuple[LocalCommit, ...],
    document_path: Path | None = None,
) -> tuple[dict[ChangeId, GeneratedDescription], dict[ChangeId, bool], Path]:
    editor_command = _resolve_editor_command(jj_client)
    if document_path is None:
        document = render_description_edit_document(
            descriptions=descriptions,
            drafts=drafts,
            changes=changes,
        )
        try:
            with tempfile.NamedTemporaryFile(
                delete=False,
                encoding="utf-8",
                mode="w",
                prefix="jj-stack-edit-",
                suffix=".md",
            ) as draft_file:
                draft_file.write(document)
                document_path = Path(draft_file.name)
        except OSError as error:
            raise CliError(t"Could not create an editor document: {error}") from error
    else:
        document_path = document_path.expanduser().resolve()
        try:
            document_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise CliError(
                t"Could not read edited pull request descriptions "
                t"{ui.code(str(document_path))}: {error}"
            ) from error

    recovery_hint = resume_edit_hint(document_path)
    try:
        completed = subprocess.run(
            [*editor_command, str(document_path)],
            check=False,
            cwd=jj_client.repo_root,
        )
    except FileNotFoundError as error:
        raise CliError(
            t"Editor {ui.cmd(editor_command[0])} was not found.",
            hint=recovery_hint,
        ) from error
    except OSError as error:
        raise CliError(
            t"Could not run editor {ui.cmd(editor_command[0])}: {error}",
            hint=recovery_hint,
        ) from error
    if completed.returncode != 0:
        raise CliError(
            t"Editor {ui.cmd(editor_command[0])} exited with status "
            t"{completed.returncode}; submit aborted.",
            hint=recovery_hint,
        )
    try:
        edited_document = document_path.read_text(encoding="utf-8")
        edited_descriptions, edited_drafts = parse_description_edit_document(
            edited_document,
            changes=changes,
        )
    except (OSError, UnicodeDecodeError) as error:
        raise CliError(
            t"Could not read edited pull request descriptions "
            t"{ui.code(str(document_path))}: {error}",
            hint=recovery_hint,
        ) from error
    except CliError as error:
        raise CliError(error.message, hint=recovery_hint) from error
    return edited_descriptions, edited_drafts, document_path
