from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.jj import JjClient


def app_context(
    tmp_path: Path,
    *,
    repo_config: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        repo_root=tmp_path,
        jj_client=JjClient(tmp_path),
        state_store=SimpleNamespace(),
        config=SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=repo_config if repo_config is not None else SimpleNamespace(),
        ),
    )


def patch_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    module,
    tmp_path: Path,
    *,
    repo_config: object | None = None,
) -> None:
    monkeypatch.setattr(
        module,
        "bootstrap_context",
        lambda **kwargs: app_context(tmp_path, repo_config=repo_config),
    )
