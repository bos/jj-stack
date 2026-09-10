from __future__ import annotations

import atexit
import contextlib
import io
import os
import pickle
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx2

import jj_stack.bootstrap
import jj_stack.github.resolution
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import CommitId, short_change_id
from jj_stack.jj.client import JjClient, PRRefUpdate
from jj_stack.models.stack import LocalStack
from jj_stack.stack.selected import select_stack_path
from jj_stack.state.store import TrackingStore

from .fake_github import (
    FakeGithubRepo,
    FakeGithubState,
    create_app,
    initialize_bare_repo,
)

_TEMPLATE_OWNER = "octo-org"
_TEMPLATE_NAME = "stacked-prs"
_TEST_JJ_IDENTITY = {
    "JJ_EMAIL": "test@example.com",
    "JJ_USER": "Test User",
}
_SHARED_TEMPLATE_ROOT: Path | None = None
_TEMPLATE_MEMO: dict[str, Path] = {}


def fake_github_client_wiring(
    fake_repo: FakeGithubRepo,
    app,
    *,
    client_type: type[GithubClient] = GithubClient,
) -> tuple[Callable[..., GithubClient], Callable[..., GithubRepoAddress]]:
    """Return the client builder and repo-address stubs for a fake server."""

    def build_github_client(*, repo: GithubRepoAddress, token: str | None = None) -> GithubClient:
        return client_type(
            httpx2.AsyncClient(
                base_url="https://api.github.test",
                transport=httpx2.ASGITransport(app=app),
            ),
            repo=repo,
        )

    def parse_github_repo(*_args, **_kwargs) -> GithubRepoAddress:
        return GithubRepoAddress(
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    return build_github_client, parse_github_repo


def patch_github_client_builders(
    monkeypatch,
    *,
    app,
    fake_repo: FakeGithubRepo,
    client_type: type[GithubClient] = GithubClient,
) -> None:
    """Point every command at the fake GitHub app through the two production seams."""

    build_github_client, parse_github_repo = fake_github_client_wiring(
        fake_repo,
        app,
        client_type=client_type,
    )
    monkeypatch.setattr("jj_stack.bootstrap.build_github_client", build_github_client)
    monkeypatch.setattr("jj_stack.github.resolution.parse_github_repo", parse_github_repo)


class OfflineGithubClient(GithubClient):
    """Fail open-PR lookups like a client with no connection to GitHub."""

    async def get_open_prs_by_head_refs(self, *, head_refs):
        raise GithubClientError("Connection refused")


def configure_fake_github_environment(
    *,
    fake_repo: FakeGithubRepo,
    monkeypatch,
    tmp_path: Path,
    extra_config_lines: list[str] | None = None,
) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_fake_github_config(
        tmp_path,
        extra_lines=extra_config_lines,
    )
    app = create_app(FakeGithubState.single_repo(fake_repo))
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
    )
    return config_path


def _copy_fake_github_repo_from_template(
    tmp_path: Path,
    template_root: Path,
) -> tuple[Path, FakeGithubRepo]:
    shutil.copytree(template_root / "repo", tmp_path / "repo")
    shutil.copytree(template_root / "remotes", tmp_path / "remotes")
    repo = tmp_path / "repo"
    git_dir = tmp_path / "remotes" / _TEMPLATE_OWNER / f"{_TEMPLATE_NAME}.git"
    run_command(["jj", "git", "remote", "set-url", "origin", str(git_dir)], repo)
    fake_repo = FakeGithubRepo(
        default_branch="main",
        git_dir=git_dir,
        name=_TEMPLATE_NAME,
        owner=_TEMPLATE_OWNER,
    )
    return repo, fake_repo


def _init_fake_github_repo_fresh(
    tmp_path: Path,
    *,
    with_remote: bool,
) -> tuple[Path, FakeGithubRepo]:
    repo = tmp_path / "repo"
    fake_repo = initialize_bare_repo(
        tmp_path / "remotes",
        owner=_TEMPLATE_OWNER,
        name=_TEMPLATE_NAME,
    )
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    write_file(repo / "README.md", "base\n")
    run_command(["jj", "commit", "-m", "base"], repo)
    run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    if with_remote:
        run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
        run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    return repo, fake_repo


def set_shared_template_root(root: Path) -> None:
    """Point template caching at a directory shared by all xdist workers.

    Configured once per session from a conftest fixture. Without it, each
    worker process falls back to building its own private template copies.
    """

    global _SHARED_TEMPLATE_ROOT
    _SHARED_TEMPLATE_ROOT = root


def _template_dir(name: str, build: Callable[[Path], None]) -> Path:
    """Return a cached template directory, building it at most once per session.

    With a shared root configured, workers coordinate through an atomic lock
    directory. One worker builds and atomically publishes the template while
    the others wait, so readers only ever observe a complete template.
    """

    cached = _TEMPLATE_MEMO.get(name)
    if cached is not None:
        return cached
    root = _SHARED_TEMPLATE_ROOT
    if root is None:
        template_root = Path(tempfile.mkdtemp(prefix=f"jjr_tpl_{name}_"))
        atexit.register(lambda: shutil.rmtree(template_root, ignore_errors=True))
        build(template_root)
        _TEMPLATE_MEMO[name] = template_root
        return template_root
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    ready = target / ".template-ready"
    lock = root / f".{name}.lock"
    deadline = time.monotonic() + 120
    while not ready.is_file():
        try:
            lock.mkdir()
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for integration template {name!r}."
                ) from None
            time.sleep(0.01)
            continue
        build_dir = root / f".{name}.build"
        try:
            if ready.is_file():
                break
            shutil.rmtree(build_dir, ignore_errors=True)
            build(build_dir)
            (build_dir / ".template-ready").touch()
            os.rename(build_dir, target)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
            lock.rmdir()
    _TEMPLATE_MEMO[name] = target
    return target


def _get_cached_template() -> Path:
    def build(root: Path) -> None:
        _init_fake_github_repo_fresh(root, with_remote=True)

    return _template_dir("base", build)


def init_fake_github_repo(
    tmp_path: Path,
    *,
    with_remote: bool = True,
) -> tuple[Path, FakeGithubRepo]:
    if not with_remote:
        return _init_fake_github_repo_fresh(tmp_path, with_remote=False)
    template_root = _get_cached_template()
    return _copy_fake_github_repo_from_template(tmp_path, template_root)


def _build_submitted_stack_template(template_root: Path, size: int) -> None:
    from jj_stack.cli import main

    prior_state_home = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(template_root / "state-home")

    saved_attrs: list[tuple[object, str, object]] = []
    try:
        repo, fake_repo = _copy_fake_github_repo_from_template(
            template_root, _get_cached_template()
        )
        for index in range(1, size + 1):
            commit_file(repo, f"feature {index}", f"feature-{index}.txt")

        app = create_app(FakeGithubState.single_repo(fake_repo))
        build_github_client, parse_github_repo = fake_github_client_wiring(fake_repo, app)

        for module, attr, new in (
            (jj_stack.bootstrap, "build_github_client", build_github_client),
            (jj_stack.github.resolution, "parse_github_repo", parse_github_repo),
        ):
            saved_attrs.append((module, attr, getattr(module, attr)))
            setattr(module, attr, new)

        config_path = write_fake_github_config(template_root)
        # The template is built lazily inside the first test that calls the
        # helper, so pytest's capsys is active. Any output produced here would
        # remain in that test's buffer and be asserted against. Future templates
        # that run production code during build must redirect stdout/stderr too.
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = main(
                ["--config-file", str(config_path), "--repository", str(repo), "submit"]
            )
        if exit_code != 0:
            raise RuntimeError(f"submitted-stack template build failed: exit {exit_code}")
        JjClient(repo).ensure_pr_branch_fetch_isolation(
            remote="origin",
        )

        (template_root / "fake_repo.pkl").write_bytes(pickle.dumps(fake_repo))
        # The template directory may be renamed after the build completes, so
        # the state-home key derived from the build path must be recorded now.
        (template_root / "repo-state-hash").write_text(_repo_state_hash(repo), encoding="utf-8")
    finally:
        for mod, attr, original in saved_attrs:
            setattr(mod, attr, original)
        if prior_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = prior_state_home


def init_fake_github_repo_with_submitted_feature(
    tmp_path: Path,
) -> tuple[Path, FakeGithubRepo]:
    return init_fake_github_repo_with_submitted_stack(tmp_path, size=1)


def selected_stack(repo: Path, revset: str | None = None) -> LocalStack:
    """Return the ordinary selected path for integration setup and assertions."""

    return select_stack_path(
        jj_client=JjClient(repo),
        revset=revset,
        state=TrackingStore.for_repo(repo).load(),
    ).stack


def init_fake_github_repo_with_submitted_stack(
    tmp_path: Path,
    *,
    size: int,
) -> tuple[Path, FakeGithubRepo]:
    """Drop-in replacement for `init_fake_github_repo + N x commit_file + submit`.

    Returns a repo with `feature 1` .. `feature <size>` already committed
    (as `feature-<n>.txt`) and submitted as PRs #1..#<size> in the returned
    `fake_repo`. Callers still need to invoke `configure_submit_environment`
    to wire the monkeypatches for their own fake_repo instance.
    """
    template = _template_dir(
        f"submitted-{size}",
        lambda root: _build_submitted_stack_template(root, size),
    )

    repo, fake_repo = _copy_fake_github_repo_from_template(tmp_path, template)

    template_state_home = template / "state-home"
    test_state_home = tmp_path / "state-home"
    if template_state_home.exists():
        shutil.copytree(template_state_home, test_state_home, dirs_exist_ok=True)
        template_repos_root = test_state_home / "jj-stack" / "repos"
        template_hash = (template / "repo-state-hash").read_text(encoding="utf-8").strip()
        test_hash = _repo_state_hash(repo)
        if template_hash != test_hash:
            src = template_repos_root / template_hash
            if src.exists():
                (template_repos_root / test_hash).mkdir(parents=True, exist_ok=True)
                for entry in src.iterdir():
                    entry.rename(template_repos_root / test_hash / entry.name)
                src.rmdir()

    pickled = pickle.loads((template / "fake_repo.pkl").read_bytes())
    pickled.git_dir = fake_repo.git_dir
    return repo, pickled


def _build_manual_pr_template(template_root: Path) -> None:
    """Build a template with `feature 1` committed and a manually created PR.

    Unlike the submitted-stack template this never runs jj-stack `main()`, so it
    has no state-home to rehome: only the jj repo, the remote, and the pickled
    `fake_repo` carry state. The PR branch exists only on the remote.
    """
    repo, fake_repo = _copy_fake_github_repo_from_template(template_root, _get_cached_template())
    commit_file(repo, "feature 1", "feature-1.txt")
    change = selected_stack(repo).head
    change_id = change.change_id
    manual_bookmark = f"jj-stack/manual-feature-{short_change_id(change_id)}"
    JjClient(repo).mutate_remote_pr_branch_refs(
        remote="origin",
        updates=(
            PRRefUpdate(
                branch=manual_bookmark,
                expected_target=None,
                desired_target=change.commit_id,
            ),
        ),
    )
    fake_repo.create_pr(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    (template_root / "fake_repo.pkl").write_bytes(pickle.dumps(fake_repo))


def init_fake_github_repo_with_manual_pr(
    tmp_path: Path,
) -> tuple[Path, FakeGithubRepo]:
    """Return a repo with `feature 1` committed and a manually created PR.

    Mirrors the manual-PR setup shared by several relink tests. Callers still
    invoke `configure_submit_environment` to wire the monkeypatches for the
    returned `fake_repo`.
    """
    template = _template_dir("manual-pr", _build_manual_pr_template)
    repo, fake_repo = _copy_fake_github_repo_from_template(tmp_path, template)
    pickled = pickle.loads((template / "fake_repo.pkl").read_bytes())
    pickled.git_dir = fake_repo.git_dir
    return repo, pickled


def _repo_state_hash(repo_root: Path) -> str:
    import hashlib

    storage_root = (repo_root / ".jj" / "repo").resolve()
    return hashlib.sha256(str(storage_root).encode("utf-8")).hexdigest()


def init_repo(
    tmp_path: Path,
    *,
    configure_trunk: bool = True,
) -> Path:
    repo = tmp_path / "repo"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    write_file(repo / "README.md", "base\n")
    run_command(["jj", "commit", "-m", "base"], repo)
    if configure_trunk:
        run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    return repo


def write_fake_github_config(tmp_path: Path, *, extra_lines: list[str] | None = None) -> Path:
    config_path = tmp_path / "jj-stack-config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[jj-stack]"]
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    write_file(config_path, "\n".join(lines) + "\n")
    return config_path


def commit_file(repo: Path, message: str, filename: str) -> None:
    write_file(repo / filename, f"{message}\n")
    run_command(["jj", "commit", "-m", message], repo)


def sign_commit(repo: Path, revset: str) -> None:
    """Sign a test commit with a disposable SSH key, without configuring trust."""

    key = repo.parent / "signing-key"
    run_command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], repo)
    run_command(
        ["jj", "--config", "signing.backend=ssh", "sign", "-r", revset, "--key", str(key)],
        repo,
    )


def jj_commit_id(repo: Path, revset: str) -> CommitId:
    return CommitId(
        run_command(
            ["jj", "log", "--no-graph", "-r", revset, "-T", "commit_id"],
            repo,
        ).stdout.strip()
    )


def expose_pr_branch_namespace(repo: Path) -> None:
    """Undo the reserved-namespace fetch exclusion, as a plain clone leaves it."""

    run_command(
        [
            "git",
            "config",
            "--replace-all",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ],
        repo,
    )


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_TEST_JJ_IDENTITY} if command[0] == "jj" else None
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=env,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{command!r} failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def remote_refs(remote: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        check=False,
        cwd=remote.parent,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(
            "['git', '--git-dir', "
            f"{str(remote)!r}, 'show-ref', '--heads'] failed:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        commit_id, ref_name = line.split(" ", maxsplit=1)
        refs[ref_name] = commit_id
    return refs


def update_remote_ref(fake_repo: FakeGithubRepo, *, branch: str, target: str) -> None:
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{branch}",
            target,
        ],
        fake_repo.git_dir.parent,
    )


def delete_remote_ref(fake_repo: FakeGithubRepo, *, branch: str) -> None:
    """Remove a branch on the remote, as GitHub does after merging when configured to."""

    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{branch}",
        ],
        fake_repo.git_dir.parent,
    )


def write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
