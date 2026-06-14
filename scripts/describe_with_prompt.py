#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if sys.platform != "win32":
    import readline
    readline.set_history_length(1000)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Interactively generate jj-stack metadata. Prints JSON with "
            "string `title` and `body` fields."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="REVSET", help="Generate metadata for one PR revset.")
    group.add_argument(
        "--stack",
        metavar="REVSET",
        help="Generate metadata for one stack revset.",
    )
    return parser.parse_args()


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


def pr_defaults(revset: str) -> tuple[str, str]:
    description = run_jj("log", "-r", revset, "--no-graph", "-T", "description")
    lines = description.splitlines()
    title = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip()
    return title, body


def stack_defaults(revset: str) -> tuple[str, str]:
    summaries = run_jj(
        "log",
        "-r",
        f"trunk()::{revset} & visible() & mutable()",
        "--no-graph",
        "-T",
        "description.first_line() ++ \"\\n\"",
    )
    changes = [line.strip() for line in summaries.splitlines() if line.strip()]
    title = "Stack summary"
    body = ""
    if changes:
        title = changes[-1]
        body = "Changes in this stack:\n" + "\n".join(f"- {change}" for change in changes)
    return title, body


def prompt_line(label: str, default: str, default_label: str) -> str:
    suffix = f" [return to use {default_label}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_body(default: str, default_label: str) -> str:
    suffix = f" [return to use {default_label}]" if default else ""
    first_line = input(f"Body{suffix}: ").rstrip()
    if default and not first_line.strip():
        return default
    if not first_line:
        return ""
    if first_line == ".":
        return ""

    print("Enter remaining body content. Finish with a single `.` on its own line.")
    lines = [first_line]
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def ensure_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Interactive describe helper requires a TTY.")


def main() -> int:
    ensure_tty()
    args = parse_args()
    mode = "pr" if args.pr is not None else "stack"
    revset = args.pr if args.pr is not None else args.stack
    if revset is None:
        raise AssertionError("argparse should guarantee a revset.")

    if mode == "pr":
        default_title, default_body = pr_defaults(revset)
        default_source = "commit"
    else:
        default_title, default_body = stack_defaults(revset)
        default_source = "stack"

    description_kind = "pull request" if mode == "pr" else "stack"
    print(f"Generating GitHub {description_kind} description for {revset!r}.")
    if default_title:
        print(f"{default_source.capitalize()} title: {default_title}")
    if default_body:
        print(f"{default_source.capitalize()} body:")
        print(default_body)

    title = prompt_line("Title", default_title, f"{default_source} title")
    body = prompt_body(default_body, f"{default_source} body")
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
