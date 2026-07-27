#!/usr/bin/env python3
"""Check whether CI covers the latest stable jj release."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

ISSUE_TITLE = "ci: jj release update needed"
CI_WORKFLOW = Path(".github/workflows/ci.yml")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether the CI jj-version matrix includes the latest stable release.",
    )
    parser.add_argument(
        "--write-issues",
        action="store_true",
        help="Create, update, or close the tracking issue on GitHub.",
    )
    return parser.parse_args()


def read_tested_versions(ci_workflow: Path) -> list[str]:
    workflow_text = ci_workflow.read_text(encoding="utf-8")
    match = re.search(r"jj-version:\s*\[(.*?)\]", workflow_text)
    if match is None:
        raise SystemExit(f"Could not find jj-version matrix in {ci_workflow}")

    versions = re.findall(r'"(v[^"]+)"', match.group(1))
    if not versions:
        raise SystemExit(f"Could not parse jj versions from {ci_workflow}")
    return versions


def github_request(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    token: str | None = None,
) -> dict:
    data = None
    headers = {"Accept": "application/vnd.github+json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def latest_jj_release() -> tuple[str, str]:
    release = github_request(
        "GET",
        "https://api.github.com/repos/jj-vcs/jj/releases/latest",
    )
    return release["tag_name"], release["html_url"]


def find_open_issue(repository: str, token: str) -> dict | None:
    encoded_query = urllib.parse.quote(
        f'repo:{repository} is:issue in:title state:open "{ISSUE_TITLE}"',
        safe="",
    )
    search = github_request(
        "GET",
        f"https://api.github.com/search/issues?q={encoded_query}",
        token=token,
    )
    return next(
        (item for item in search.get("items", []) if item.get("title") == ISSUE_TITLE),
        None,
    )


def tracking_issue_body(
    *,
    latest_version: str,
    latest_url: str,
    tested_versions: list[str],
    ci_workflow: Path,
) -> str:
    tested_display = ", ".join(tested_versions)
    return (
        "A newer stable `jj` release is available.\n\n"
        f"- Latest stable release: `{latest_version}`\n"
        f"- Release notes: {latest_url}\n"
        f"- Versions currently tested in CI: `{tested_display}`\n\n"
        "Update both of these files in the same change:\n"
        f"- `{ci_workflow}`: add `{latest_version}` to the `jj-version` matrix\n"
        "- `tools/install-jj-release.sh`: add SHA-256 checksums for every "
        "supported platform for the new release\n"
    )


def sync_issue_state(
    *,
    repository: str,
    token: str,
    latest_version: str,
    latest_url: str,
    tested_versions: list[str],
    ci_workflow: Path,
) -> None:
    issue = find_open_issue(repository, token)
    if latest_version in tested_versions:
        if issue is not None:
            github_request(
                "PATCH",
                issue["url"],
                payload={"state": "closed"},
                token=token,
            )
            print(f"Closed resolved issue #{issue['number']}.")
        return

    body = tracking_issue_body(
        latest_version=latest_version,
        latest_url=latest_url,
        tested_versions=tested_versions,
        ci_workflow=ci_workflow,
    )
    if issue is None:
        created = github_request(
            "POST",
            f"https://api.github.com/repos/{repository}/issues",
            payload={"title": ISSUE_TITLE, "body": body},
            token=token,
        )
        print(f"Opened issue #{created['number']} for jj {latest_version}.")
        return

    github_request(
        "PATCH",
        issue["url"],
        payload={"body": body},
        token=token,
    )
    print(f"Updated existing issue #{issue['number']}.")


def main() -> int:
    args = parse_args()
    tested_versions = read_tested_versions(CI_WORKFLOW)
    latest_version, latest_url = latest_jj_release()
    tested_display = ", ".join(tested_versions)
    if latest_version in tested_versions:
        print(
            f"Latest jj release {latest_version} is already covered by the "
            f"CI matrix: {tested_display}."
        )
    else:
        print(f"Latest jj release {latest_version} is not in the CI matrix: {tested_display}.")

    if not args.write_issues:
        return 0 if latest_version in tested_versions else 1

    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("REPOSITORY")
    if not token or not repository:
        raise SystemExit("--write-issues requires GITHUB_TOKEN and REPOSITORY in the environment")
    sync_issue_state(
        repository=repository,
        token=token,
        latest_version=latest_version,
        latest_url=latest_url,
        tested_versions=tested_versions,
        ci_workflow=CI_WORKFLOW,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
