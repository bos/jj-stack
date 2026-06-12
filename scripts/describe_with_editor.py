#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

COMMENT_BLOCK_RE = re.compile(r"<!--\s*jj-stack:.*?-->", re.DOTALL)
COMMENT_START = "<!-- jj-stack:"
COMMENT_END = "-->"
STACK_INPUT_ENV = "JJ_STACK_INPUT_FILE"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Interactively author jj-stack metadata with $EDITOR. Prints JSON "
            "with string `title` and `body` fields."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="REVSET", help="Generate metadata for one PR revset.")
    group.add_argument(
        "--stack",
        metavar="REVSET",
        help="Generate metadata for one stack revset.",
    )
    return parser.parse_args(argv)


def run_jj(*args: str) -> str:
    completed = subprocess.run(
        ["jj", *args],
        capture_output=True,
        check=False,
        cwd=Path.cwd(),
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "unknown jj failure"
        raise SystemExit(detail)
    return completed.stdout


def pr_context_lines(revset: str) -> list[str]:
    description = run_jj("log", "-r", revset, "--no-graph", "-T", "description")
    lines = description.rstrip().splitlines()
    if not lines:
        return ["Commit description is empty."]
    return lines


def stack_context_lines(revset: str) -> list[str]:
    generated_context = generated_stack_context_lines()
    if generated_context is not None:
        return generated_context
    return local_stack_context_lines(revset)


def generated_stack_context_lines() -> list[str] | None:
    input_path = os.environ.get(STACK_INPUT_ENV)
    if not input_path:
        return None
    try:
        payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    revisions = payload.get("revisions")
    if not isinstance(revisions, list):
        return None

    lines = ["Generated pull request descriptions for this stack, bottom to top:"]
    for index, revision in enumerate(revisions, start=1):
        if not isinstance(revision, dict):
            return None
        title = revision.get("title")
        body = revision.get("body")
        diffstat = revision.get("diffstat")
        if not isinstance(title, str) or not isinstance(body, str):
            return None
        lines.extend(["", f"{index}. {title}"])
        if body.strip():
            lines.extend(["", *body.strip().splitlines()])
        if isinstance(diffstat, str) and diffstat.strip():
            lines.extend(["", "Diffstat:", *diffstat.strip().splitlines()])
    return lines


def local_stack_context_lines(revset: str) -> list[str]:
    entries = run_jj(
        "log",
        "-r",
        f"trunk()::{revset} & visible() & mutable()",
        "--no-graph",
        "-T",
        'change_id.short() ++ "\\0" ++ description ++ "\\u0001"',
    ).split("\u0001")
    lines = ["Commit descriptions in this stack, bottom to top:"]
    for index, entry in enumerate(reversed([entry for entry in entries if entry]), start=1):
        change_id, _, description = entry.partition("\0")
        description_lines = description.rstrip().splitlines()
        lines.extend(["", f"{index}. {change_id}"])
        if description_lines:
            lines.extend(description_lines)
        else:
            lines.append("Commit description is empty.")
    return lines


def initial_editor_text(*, context_lines: list[str], mode: str, revset: str) -> str:
    subject = "pull request" if mode == "pr" else "stack summary"
    context_label = "commit description" if mode == "pr" else "stack context"
    comment_lines = [
        f"Write the {subject} title on the first non-comment line.",
        "Remaining non-comment lines become the body.",
        "Leave the file blank, aside from these comments, to abort.",
        "",
        f"{context_label.capitalize()} for {revset}:",
        *context_lines,
    ]
    return "\n".join(["", COMMENT_START, *comment_lines, COMMENT_END, ""])


def run_editor(path: Path) -> int:
    editor = os.environ.get("EDITOR")
    if editor is None or not editor.strip():
        print("EDITOR environment variable is not set.", file=sys.stderr)
        return 1
    try:
        command = [*shlex.split(editor), str(path)]
    except ValueError as error:
        print(f"Could not parse EDITOR: {error}", file=sys.stderr)
        return 1
    if not command:
        print("EDITOR environment variable is not set.", file=sys.stderr)
        return 1

    try:
        with Path("/dev/tty").open("r+b", buffering=0) as tty:
            return run_editor_command(command, stdin=tty, stdout=tty, stderr=tty)
    except OSError:
        return run_editor_command(command, stdin=None, stdout=None, stderr=None)


def run_editor_command(
    command: list[str],
    *,
    stdin: Any,
    stdout: Any,
    stderr: Any,
) -> int:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
    except FileNotFoundError as error:
        print(f"Could not run editor {command[0]!r}: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Could not run editor {command[0]!r}: {error}", file=sys.stderr)
        return 1

    if completed.returncode != 0:
        print(f"Editor exited with status {completed.returncode}.", file=sys.stderr)
        return completed.returncode or 1
    return 0


def parse_edited_description(text: str) -> tuple[str, str] | None:
    lines = [line.rstrip() for line in COMMENT_BLOCK_RE.sub("", text).splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None

    title = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    return title, body


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "pr" if args.pr is not None else "stack"
    revset = args.pr if args.pr is not None else args.stack
    if revset is None:
        raise AssertionError("argparse should guarantee a revset.")

    if mode == "pr":
        context_lines = pr_context_lines(revset)
    else:
        context_lines = stack_context_lines(revset)

    with tempfile.TemporaryDirectory(prefix="jj-stack-editor-") as tempdir:
        description_path = Path(tempdir) / f"{mode}-description.md"
        description_path.write_text(
            initial_editor_text(context_lines=context_lines, mode=mode, revset=revset),
            encoding="utf-8",
        )
        editor_status = run_editor(description_path)
        if editor_status != 0:
            return editor_status
        parsed = parse_edited_description(description_path.read_text(encoding="utf-8"))

    if parsed is None:
        print("No description was written.", file=sys.stderr)
        return 1

    title, body = parsed
    print(json.dumps({"title": title, "body": body}))
    return 0


def run() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
