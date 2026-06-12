#!/usr/bin/env python3
# Tested with Codex CLI 0.116.0.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CODEX_MODEL = "gpt-5.4-mini"
MAX_PR_CONTEXT_BYTES = 900_000
STACK_INPUT_ENV = "JJ_REVIEW_STACK_INPUT_FILE"

PROMPT_TEMPLATE = """\
{task}

Return JSON with exactly two string fields:
- `title`: a one-line title
- `body`: GitHub-flavored Markdown

- Do not mention AI.
- Do not wrap the JSON in code fences.
- Keep the title concise, specific, and informative.
- Prefer reviewer-useful summaries over diff narration.
- Explain what changed, why it changed, risks, and testing when known.
{extra_guidance}

Use the review context below:
{context}
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate jj-stack metadata with Codex CLI. Prints JSON with "
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


def build_context(mode: str, revset: str) -> str:
    if mode == "pr":
        return build_pr_context(revset)
    return build_stack_context(revset)


def build_pr_context(revset: str) -> str:
    raw_context = run_jj("show", "--git", "-r", revset).strip()
    return truncate_context(raw_context, max_bytes=MAX_PR_CONTEXT_BYTES)


def build_stack_context(revset: str) -> str:
    generated_context = generated_stack_context()
    if generated_context is not None:
        return generated_context
    return local_stack_context(revset)


def generated_stack_context() -> str | None:
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
        lines.extend([f"{index}. {title}"])
        if body.strip():
            lines.extend(["", body.strip()])
        if isinstance(diffstat, str) and diffstat.strip():
            lines.extend(["", "Diffstat:", diffstat.strip()])
        lines.append("")
    return "\n".join(lines).strip()


def local_stack_context(revset: str) -> str:
    revisions = stack_revisions(revset)
    lines = ["Source-control summaries for this stack, bottom to top:"]
    for index, revision in enumerate(revisions, start=1):
        lines.append(f"{index}. {revision['title']}")
        if revision["body"]:
            lines.extend(["", revision["body"]])
        if revision["diffstat"]:
            lines.extend(["", "Diffstat:", revision["diffstat"]])
        lines.append("")
    return "\n".join(lines).strip()


def stack_revisions(revset: str) -> list[dict[str, str]]:
    entries = run_jj(
        "log",
        "-r",
        f"trunk()::{revset} & visible() & mutable()",
        "--no-graph",
        "-T",
        'change_id ++ "\\0" ++ description ++ "\\u0001"',
    ).split("\u0001")
    revisions: list[dict[str, str]] = []
    for entry in reversed(entries):
        if not entry:
            continue
        change_id, _, description = entry.partition("\0")
        normalized_description = description.strip()
        if not normalized_description:
            title = change_id[:8]
            body = ""
        else:
            lines = normalized_description.splitlines()
            title = lines[0].strip() or change_id[:8]
            body = "\n".join(line.rstrip() for line in lines[1:]).strip()
        revisions.append(
            {
                "body": body,
                "diffstat": diffstat_for_revision(change_id),
                "title": title,
            }
        )
    return revisions


def diffstat_for_revision(revset: str) -> str:
    lines = run_jj("show", "--stat", "-r", revset).rstrip().splitlines()
    diffstat_lines: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            if diffstat_lines:
                break
            continue
        diffstat_lines.append(line)
    return "\n".join(reversed(diffstat_lines))


def truncate_context(text: str, *, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return (
        f"[review context truncated to the first {max_bytes} bytes; original size "
        f"{len(encoded)} bytes]\n\n{truncated}"
    )


def stack_commit_count(revset: str) -> int:
    return int(
        run_jj(
            "log",
            "--count",
            "-r",
            f"trunk()::{revset} & visible() & mutable()",
        ).strip()
    )


def build_prompt(mode: str, revset: str, context: str) -> str:
    if mode == "pr":
        task = "Write a GitHub pull request title and body for a human reviewer."
        extra_guidance = (
            "- Optimize for a reviewer who wants to understand one change quickly."
        )
    else:
        task = "Write a GitHub stack summary for a human reviewer."
        commit_count = stack_commit_count(revset)
        if commit_count == 1:
            extra_guidance = "\n".join(
                [
                    "- This stack contains exactly one commit.",
                    "- Describe that one change directly.",
                    "- Do not invent a broader series or mention multiple commits.",
                    "- The stack helper is normally skipped for a one-commit stack.",
                ]
            )
        else:
            extra_guidance = "\n".join(
                [
                    f"- This stack contains {commit_count} commits.",
                    "- Summarize the series as a whole, not just the top commit.",
                    "- Explain how the changes in the stack fit together.",
                    "- The body will appear above the selected head PR's stack-navigation "
                    "comment.",
                ]
            )
    return PROMPT_TEMPLATE.format(
        context=context or "(no source control context available)",
        extra_guidance=extra_guidance,
        task=task,
    )


def main() -> int:
    args = parse_args()
    mode = "pr" if args.pr is not None else "stack"
    revset = args.pr if args.pr is not None else args.stack
    if revset is None:
        raise AssertionError("argparse should guarantee a revset.")

    prompt = build_prompt(mode, revset, build_context(mode, revset))
    codex_bin = os.environ.get("JJ_REVIEW_CODEX_BIN", "codex")
    with tempfile.TemporaryDirectory(prefix="jj-stack-codex-") as tempdir:
        tempdir_path = Path(tempdir)
        schema_path = tempdir_path / "schema.json"
        output_path = tempdir_path / "output.json"
        schema_path.write_text(json.dumps(SCHEMA), encoding="utf-8")

        completed = subprocess.run(
            [
                codex_bin,
                "-a",
                "never",
                "--model",
                CODEX_MODEL,
                "-s",
                "read-only",
                "exec",
                "-C",
                str(Path.cwd()),
                "--output-schema",
                str(schema_path),
                "--color",
                "never",
                "-o",
                str(output_path),
                "-",
            ],
            capture_output=True,
            check=False,
            cwd=Path.cwd(),
            input=prompt,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip() or "Codex failed"
            print(detail, file=sys.stderr)
            return completed.returncode or 1

        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print("Codex did not write an output file.", file=sys.stderr)
            return 1

    if not isinstance(payload, dict) or not all(
        isinstance(payload.get(field), str) for field in ("title", "body")
    ):
        print(
            "Codex did not return a JSON object with string title/body fields.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"title": payload["title"], "body": payload["body"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
