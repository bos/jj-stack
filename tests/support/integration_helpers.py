from __future__ import annotations

import atexit
import contextlib
import importlib
import io
import os
import pickle
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpxyz

from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress

from .fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)

_TEMPLATE_OWNER = "octo-org"
_TEMPLATE_NAME = "stacked-review"
_SHARED_TEMPLATE_ROOT: Path | None = None
_TEMPLATE_MEMO: dict[str, Path] = {}
_SUBMIT_CONFIG_MODULES = (
    "jj_stack.commands.submit.command",
    "jj_stack.commands.relink",
    "jj_stack.commands.unstack",
    "jj_stack.commands.close_orphan",
    "jj_stack.commands.cleanup.command",
    "jj_stack.commands.land.command",
    "jj_stack.commands.land.recovery",
    "jj_stack.review.status",
)


def configure_fake_github_environment(
    *,
    command_modules: tuple[str, ...],
    fake_repo: FakeGithubRepository,
    monkeypatch,
    tmp_path: Path,
    extra_config_lines: list[str] | None = None,
) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_fake_github_config(
        tmp_path,
        fake_repo,
        extra_lines=extra_config_lines,
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, repository: GithubRepoAddress) -> GithubClient:
        return GithubClient(
            httpxyz.AsyncClient(
                base_url="https://api.github.test",
                transport=httpxyz.ASGITransport(app=app),
            ),
            repository=repository,
        )

    def parse_github_repo(*_args, **_kwargs) -> GithubRepoAddress:
        return GithubRepoAddress(
            host="github.test",
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    resolution_module = importlib.import_module("jj_stack.github.resolution")
    monkeypatch.setattr(resolution_module, "parse_github_repo", parse_github_repo)
    for module in command_modules:
        module_object = importlib.import_module(module)
        monkeypatch.setattr(
            module_object,
            "build_github_client",
            build_github_client,
            raising=False,
        )
        monkeypatch.setattr(module_object, "parse_github_repo", parse_github_repo, raising=False)
        monkeypatch.setattr(
            module_object, "require_github_repo", parse_github_repo, raising=False
        )
    return config_path


def _copy_fake_github_repo_from_template(
    tmp_path: Path,
    template_root: Path,
) -> tuple[Path, FakeGithubRepository]:
    shutil.copytree(template_root / "repo", tmp_path / "repo")
    shutil.copytree(template_root / "remotes", tmp_path / "remotes")
    repo = tmp_path / "repo"
    git_dir = tmp_path / "remotes" / _TEMPLATE_OWNER / f"{_TEMPLATE_NAME}.git"
    run_command(["jj", "git", "remote", "set-url", "origin", str(git_dir)], repo)
    fake_repo = FakeGithubRepository(
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
) -> tuple[Path, FakeGithubRepository]:
    repo = tmp_path / "repo"
    fake_repo = initialize_bare_repository(
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

    With a shared root configured, workers coordinate through the filesystem:
    a template is built in a process-private directory and atomically renamed
    into place, so concurrent builders waste at most one redundant build and
    readers only ever observe complete templates.
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
    if not (target / ".template-ready").is_file():
        build_dir = root / f"{name}.build-{os.getpid()}"
        build(build_dir)
        (build_dir / ".template-ready").touch()
        try:
            os.rename(build_dir, target)
        except OSError:
            shutil.rmtree(build_dir, ignore_errors=True)
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
) -> tuple[Path, FakeGithubRepository]:
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

        app = create_app(FakeGithubState.single_repository(fake_repo))

        def build_github_client(*, repository: GithubRepoAddress) -> GithubClient:
            return GithubClient(
                httpxyz.AsyncClient(
                    base_url="https://api.github.test",
                    transport=httpxyz.ASGITransport(app=app),
                ),
                repository=repository,
            )

        def parse_github_repo(*_args, **_kwargs) -> GithubRepoAddress:
            return GithubRepoAddress(
                host="github.test", owner=fake_repo.owner, repo=fake_repo.name
            )

        for mod_name in _SUBMIT_CONFIG_MODULES:
            mod = importlib.import_module(mod_name)
            for attr, new in (
                ("build_github_client", build_github_client),
                ("parse_github_repo", parse_github_repo),
                ("require_github_repo", parse_github_repo),
            ):
                if hasattr(mod, attr):
                    saved_attrs.append((mod, attr, getattr(mod, attr)))
                    setattr(mod, attr, new)

        config_path = write_fake_github_config(template_root, fake_repo)
        # The template is built lazily inside the first test that calls the
        # helper, so pytest's capsys is active. Any output produced here would
        # land in that test's buffer and be asserted against. Future templates
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

        (template_root / "fake_repo.pkl").write_bytes(pickle.dumps(fake_repo))
        # The template directory may be renamed after the build completes, so
        # the state-home key derived from the build path must be recorded now.
        (template_root / "repo-state-hash").write_text(
            _repo_state_hash(repo), encoding="utf-8"
        )
    finally:
        for mod, attr, original in saved_attrs:
            setattr(mod, attr, original)
        if prior_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = prior_state_home


def init_fake_github_repo_with_submitted_feature(
    tmp_path: Path,
) -> tuple[Path, FakeGithubRepository]:
    return init_fake_github_repo_with_submitted_stack(tmp_path, size=1)


def init_fake_github_repo_with_submitted_stack(
    tmp_path: Path,
    *,
    size: int,
) -> tuple[Path, FakeGithubRepository]:
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
    """Build a template with `feature 1` committed, `review/manual-feature-1`
    pushed, and a manually-created PR #1 targeting it.

    Unlike the submitted-stack template this never runs jj-stack `main()`, so it
    has no state-home to rehome: only the jj repo, the remote, and the pickled
    `fake_repo` carry state. The manual bookmark is left in place; tests that
    need it forgotten do so as a cheap per-test step.
    """
    repo, fake_repo = _copy_fake_github_repo_from_template(
        template_root, _get_cached_template()
    )
    manual_bookmark = "review/manual-feature-1"
    commit_file(repo, "feature 1", "feature-1.txt")
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", "@-"], repo)
    run_command(
        ["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo
    )
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    (template_root / "fake_repo.pkl").write_bytes(pickle.dumps(fake_repo))


def init_fake_github_repo_with_manual_pr(
    tmp_path: Path,
) -> tuple[Path, FakeGithubRepository]:
    """Return a repo with `feature 1` committed, `review/manual-feature-1`
    pushed, and a manual PR #1 already targeting it (bookmark still present).

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


def write_fake_github_config(
    tmp_path: Path, _fake_repo: FakeGithubRepository, *, extra_lines: list[str] | None = None
) -> Path:
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


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, check=False, cwd=cwd, text=True)
    if completed.returncode != 0:
        raise AssertionError(
            f"{command!r} failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
