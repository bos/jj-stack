from pathlib import Path

import pytest

from ..support import integration_helpers


@pytest.fixture(autouse=True, scope="session")
def _share_repo_templates_across_workers(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Let all xdist workers reuse one set of cached repo templates.

    Each worker's base temp lives under the session temp root (`popen-gwN`
    subdirectories), so the parent directory is shared across workers and
    outlives no test. Without this, every worker rebuilds every template.
    """

    base = tmp_path_factory.getbasetemp()
    root = base.parent if base.name.startswith("popen-") else base
    integration_helpers.set_shared_template_root(root / "jj-stack-templates")


@pytest.fixture(autouse=True)
def _isolate_jj_user_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    xdg_config_home = tmp_path / "xdg-config"
    home.mkdir()
    xdg_config_home.mkdir()
    jj_config = tmp_path / "jj-test-config.toml"
    jj_config.write_text(
        '[revset-aliases]\n"trunk()" = "main"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    monkeypatch.setenv("JJ_USER", "Test User")
    monkeypatch.setenv("JJ_EMAIL", "test@example.com")
    monkeypatch.setenv("JJ_CONFIG", str(jj_config))
