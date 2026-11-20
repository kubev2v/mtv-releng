"""Tests for the btrfs_sync rebase fallback."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipelines.btrfs_sync import _deepen, _rebase_onto


# --- _deepen ------------------------------------------------------------------


def test_deepen_unshallows_a_shallow_repo():
    git = MagicMock()
    git.is_shallow.return_value = True
    _deepen(git, "main", "internal")
    git.fetch.assert_called_once_with(
        "main", origin="internal", unshallow=True
    )


def test_deepen_plain_fetch_on_a_complete_repo():
    git = MagicMock()
    git.is_shallow.return_value = False
    _deepen(git, "main", "origin")
    git.fetch.assert_called_once_with("main", origin="origin", depth=None)


# --- _rebase_onto -------------------------------------------------------------


def test_rebase_succeeds_first_try():
    git = MagicMock()
    _rebase_onto(git, "main", "internal", 50)
    git.rebase.assert_called_once_with("internal/main")
    git.rebase_abort.assert_not_called()
    git.fetch.assert_not_called()


def test_conflict_with_shallow_fetch_deepens_both_sides():
    git = MagicMock()
    git.rebase.side_effect = [RuntimeError("CONFLICT"), None]
    # Shallow before the first deepen; the --unshallow lifts the boundary,
    # so the second deepen is a plain full fetch
    git.is_shallow.side_effect = [True, False]
    _rebase_onto(git, "main", "internal", 50)
    git.rebase_abort.assert_called_once_with()
    assert git.fetch.call_args_list == [
        call("main", origin="origin", unshallow=True),
        call("main", origin="internal", depth=None),
    ]
    assert git.rebase.call_count == 2


def test_conflict_on_created_from_main_deepens_the_source_branch():
    # release-9.9 was created from main: origin has no such branch, so the
    # recovery must deepen origin/main, not origin/release-9.9
    git = MagicMock()
    git.rebase.side_effect = [RuntimeError("CONFLICT"), None]
    git.is_shallow.return_value = False
    _rebase_onto(
        git, "release-9.9", "internal", 50, source_branch="main"
    )
    assert git.fetch.call_args_list == [
        call("main", origin="origin", depth=None),
        call("release-9.9", origin="internal", depth=None),
    ]


def test_conflict_rebasing_onto_origin_deepens_only_origin():
    git = MagicMock()
    git.rebase.side_effect = [RuntimeError("CONFLICT"), None]
    git.is_shallow.return_value = True
    _rebase_onto(git, "main", "origin", 50)
    assert git.fetch.call_args_list == [
        call("main", origin="origin", unshallow=True),
    ]


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
    git.is_shallow.return_value = True
    with pytest.raises(RuntimeError):
        _rebase_onto(git, "release-2.12", "internal", 50)
    git.rebase_abort.assert_called_once_with()
    assert git.rebase.call_count == 2
