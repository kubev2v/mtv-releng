"""Tests for the btrfs_sync rebase fallback."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipelines.btrfs_sync import _rebase_onto


def test_rebase_succeeds_first_try():
    git = MagicMock()
    _rebase_onto(git, "main", "internal", 50)
    git.rebase.assert_called_once_with("internal/main")
    git.rebase_abort.assert_not_called()
    git.fetch.assert_not_called()


def test_conflict_with_shallow_fetch_retries_full_depth():
    git = MagicMock()
    git.rebase.side_effect = [RuntimeError("CONFLICT"), None]
    _rebase_onto(git, "main", "internal", 50)
    # aborted, both sides refetched in full, then rebased again
    git.rebase_abort.assert_called_once_with()
    assert git.fetch.call_args_list == [
        call("main", origin="origin", depth=None),
        call("main", origin="internal", depth=None),
    ]
    assert git.rebase.call_count == 2


def test_conflict_at_full_depth_raises_immediately():
    git = MagicMock()
    git.rebase.side_effect = RuntimeError("CONFLICT")
    with pytest.raises(RuntimeError):
        _rebase_onto(git, "main", "internal", None)
    git.rebase_abort.assert_not_called()
    git.fetch.assert_not_called()


def test_conflict_persists_at_full_depth_raises():
    git = MagicMock()
    git.rebase.side_effect = [RuntimeError("CONFLICT"), RuntimeError("CONFLICT")]
    with pytest.raises(RuntimeError):
        _rebase_onto(git, "release-2.12", "internal", 50)
    git.rebase_abort.assert_called_once_with()
    assert git.rebase.call_count == 2
