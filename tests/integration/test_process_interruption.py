from __future__ import annotations

import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore
from tests.integration.submit_command_helpers import approve_pull_requests, read_remote_ref
from tests.support.integration_helpers import (
    init_fake_github_repo_with_submitted_stack,
    write_fake_github_config,
)
from tests.support.process_interruption import FAULT_EXIT

_HELPER = Path(__file__).parents[1] / "support" / "process_interruption.py"
_REPO_ROOT = Path(__file__).parents[2]


@pytest.mark.merger_replacement
@pytest.mark.parametrize(
    "fault",
    ("trunk_push", "accepted_merge", "retirement_save"),
)
def test_process_death_converges_from_observable_land_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    size = 2 if fault == "accepted_merge" else 1
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=size)
    approve_pull_requests(fake_repo, *range(1, size + 1))
    revisions = JjClient(repo).discover_review_stack().revisions
    config_path = write_fake_github_config(tmp_path, fake_repo)
    service_path = tmp_path / "fake-github.pkl"
    service_path.write_bytes(pickle.dumps(fake_repo))
    environment = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}

    interrupted = _run_child(
        fault=fault,
        mode="fault",
        repo=repo,
        config_path=config_path,
        service_path=service_path,
        state_home=state_home,
        environment=environment,
    )
    assert interrupted.returncode == FAULT_EXIT, (interrupted.stdout, interrupted.stderr)

    residue_fake = pickle.loads(service_path.read_bytes())
    residue_state = ReviewStateStore.for_repo(repo).load()
    landed = revisions[0]
    accepted_result = (
        residue_fake.pull_requests[1].merge_commit_sha
        if fault == "accepted_merge"
        else landed.commit_id
    )
    assert accepted_result is not None
    assert read_remote_ref(residue_fake.git_dir, "main") == accepted_result
    assert landed.change_id in residue_state.review_identities
    assert landed.change_id in residue_state.submitted_baselines
    if fault == "accepted_merge":
        assert residue_fake.pull_requests[1].state == "closed"
        assert residue_fake.pull_requests[1].merged_at is not None

    recovered = _run_child(
        fault=fault,
        mode="recover",
        repo=repo,
        config_path=config_path,
        service_path=service_path,
        state_home=state_home,
        environment=environment,
    )
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)

    final_fake = pickle.loads(service_path.read_bytes())
    final_state = ReviewStateStore.for_repo(repo).load()
    client = JjClient(repo)
    assert read_remote_ref(final_fake.git_dir, "main") == accepted_result
    assert client.resolve_revision("trunk()").commit_id == accepted_result
    assert final_fake.pull_requests[1].state == "closed"
    assert final_fake.pull_requests[1].merged_at is not None
    close_events = [
        event
        for event in final_fake.pull_request_events
        if event.pull_request_number == 1 and event.kind == "state"
    ]
    assert len(close_events) == 1
    assert landed.change_id not in final_state.review_identities
    assert landed.change_id not in final_state.submitted_baselines
    if fault == "accepted_merge":
        survivor = revisions[1]
        surviving_revision = client.resolve_revision(survivor.change_id)
        identity = final_state.review_identities[survivor.change_id]
        baseline = final_state.submitted_baselines[survivor.change_id]
        bookmark_state = client.get_bookmark_state(identity.head_ref)
        assert set(final_fake.pull_requests) == {1, 2}
        assert final_fake.pull_requests[2].state == "open"
        assert final_fake.pull_requests[2].base_ref == "main"
        assert final_fake.pull_requests[2].head_ref == identity.head_ref
        assert surviving_revision.only_parent_commit_id() == accepted_result
        assert baseline.commit_id == surviving_revision.commit_id
        assert bookmark_state.local_target == surviving_revision.commit_id
        remote_state = bookmark_state.remote_target("origin")
        assert remote_state is not None
        assert remote_state.targets == (surviving_revision.commit_id,)
        assert remote_state.tracking_targets == (surviving_revision.commit_id,)
        assert (
            read_remote_ref(final_fake.git_dir, identity.head_ref)
            == surviving_revision.commit_id
        )
    else:
        assert not final_state.review_identities
        assert not final_state.submitted_baselines


def _run_child(
    *,
    fault: str,
    mode: str,
    repo: Path,
    config_path: Path,
    service_path: Path,
    state_home: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_HELPER),
            fault,
            mode,
            str(repo),
            str(config_path),
            str(service_path),
            str(state_home),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=environment,
        text=True,
    )
