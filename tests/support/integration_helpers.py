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
from pathlib import Path

import httpxyz

from jj_review.github.client import GithubClient
from jj_review.github.resolution import ParsedGithubRepo

from .fake_github import (
    FakeGithubRepository,
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)

_TEMPLATE_OWNER = "octo-org"
_TEMPLATE_NAME = "stacked-review"
_CACHED_TEMPLATE: Path | None = None
_CACHED_SUBMITTED_FEATURE_TEMPLATE: Path | None = None
_SUBMIT_CONFIG_MODULES = (
    "jj_review.commands.submit.command",
    "jj_review.commands.relink",
    "jj_review.commands.unstack",
    "jj_review.commands.close_orphan",
    "jj_review.commands.cleanup.command",
    "jj_review.commands.land.command",
    "jj_review.review.status",
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

    def build_github_client(*, base_url: str) -> GithubClient:
        return GithubClient(
            httpxyz.AsyncClient(
                base_url=base_url,
                transport=httpxyz.ASGITransport(app=app),
            )
        )

    def parse_github_repo(*_args, **_kwargs) -> ParsedGithubRepo:
        return ParsedGithubRepo(
            host="github.test",
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    resolution_module = importlib.import_module("jj_review.github.resolution")
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


def _get_cached_template() -> Path:
    global _CACHED_TEMPLATE
    if _CACHED_TEMPLATE is None:
        template_root = Path(tempfile.mkdtemp(prefix="jjr_tpl_"))
        atexit.register(lambda: shutil.rmtree(template_root, ignore_errors=True))
        _init_fake_github_repo_fresh(template_root, with_remote=True)
        _CACHED_TEMPLATE = template_root
    return _CACHED_TEMPLATE


def init_fake_github_repo(
    tmp_path: Path,
    *,
    with_remote: bool = True,
) -> tuple[Path, FakeGithubRepository]:
    if not with_remote:
        return _init_fake_github_repo_fresh(tmp_path, with_remote=False)
    template_root = _get_cached_template()
    return _copy_fake_github_repo_from_template(tmp_path, template_root)


def _build_submitted_feature_template(template_root: Path) -> None:
    from jj_review.cli import main

    prior_state_home = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(template_root / "state-home")

    saved_attrs: list[tuple[object, str, object]] = []
    try:
        repo, fake_repo = _copy_fake_github_repo_from_template(
            template_root, _get_cached_template()
        )
        commit_file(repo, "feature 1", "feature-1.txt")

        app = create_app(FakeGithubState.single_repository(fake_repo))

        def build_github_client(*, base_url: str) -> GithubClient:
            return GithubClient(
                httpxyz.AsyncClient(
                    base_url=base_url,
                    transport=httpxyz.ASGITransport(app=app),
                )
            )

        def parse_github_repo(*_args, **_kwargs) -> ParsedGithubRepo:
            return ParsedGithubRepo(
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
            raise RuntimeError(f"submitted-feature template build failed: exit {exit_code}")

        (template_root / "fake_repo.pkl").write_bytes(pickle.dumps(fake_repo))
    finally:
        for mod, attr, original in saved_attrs:
            setattr(mod, attr, original)
        if prior_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = prior_state_home


def _get_cached_submitted_feature_template() -> Path:
    global _CACHED_SUBMITTED_FEATURE_TEMPLATE
    if _CACHED_SUBMITTED_FEATURE_TEMPLATE is None:
        template_root = Path(tempfile.mkdtemp(prefix="jjr_tpl_sub_"))
        atexit.register(lambda: shutil.rmtree(template_root, ignore_errors=True))
        _build_submitted_feature_template(template_root)
        _CACHED_SUBMITTED_FEATURE_TEMPLATE = template_root
    return _CACHED_SUBMITTED_FEATURE_TEMPLATE


def init_fake_github_repo_with_submitted_feature(
    tmp_path: Path,
) -> tuple[Path, FakeGithubRepository]:
    """Drop-in replacement for `init_fake_github_repo + commit_file("feature 1", ...) + submit`.

    Returns a repo with `feature 1` already committed and submitted as PR #1
    in the returned `fake_repo`. Callers still need to invoke
    `configure_submit_environment` to wire the monkeypatches for their own
    fake_repo instance.
    """
    template = _get_cached_submitted_feature_template()
    template_repo = template / "repo"

    repo, fake_repo = _copy_fake_github_repo_from_template(tmp_path, template)

    template_state_home = template / "state-home"
    test_state_home = tmp_path / "state-home"
    if template_state_home.exists():
        shutil.copytree(template_state_home, test_state_home, dirs_exist_ok=True)
        template_repos_root = test_state_home / "jj-review" / "repos"
        template_hash = _repo_state_hash(template_repo)
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
    config_path = tmp_path / "jj-review-config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[jj-review]"]
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
