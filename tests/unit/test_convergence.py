from jj_stack.review.convergence import (
    _workspace_disposal_command,
    _workspace_move_command,
)


def test_workspace_disposal_uses_each_platforms_recoverable_delete() -> None:
    posix_path = "/tmp/other workspace"
    assert "jj new 'trunk()'" in _workspace_move_command(root=posix_path, platform="darwin")
    assert "trash '/tmp/other workspace'" in _workspace_disposal_command(
        name="other", root=posix_path, platform="darwin"
    )
    assert "gio trash '/tmp/other workspace'" in _workspace_disposal_command(
        name="other", root=posix_path, platform="linux"
    )
    windows_path = r"C:\work\other's workspace"
    windows_move = _workspace_move_command(root=windows_path, platform="win32")
    windows_disposal = _workspace_disposal_command(
        name="other's", root=windows_path, platform="win32"
    )
    assert "Push-Location -LiteralPath" in windows_move
    assert "SendToRecycleBin" in windows_disposal
