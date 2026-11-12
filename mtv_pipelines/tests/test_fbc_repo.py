import json
import logging
import os
import tempfile

from models.fbc_repo import FBCRepo


def _repo(entries: list[dict]) -> FBCRepo:
    repo = FBCRepo.__new__(FBCRepo)
    repo.tmp_dir = tempfile.TemporaryDirectory()
    repo.logger = logging.getLogger("test")
    os.makedirs(f"{repo.tmp_dir.name}/v4.22")
    repo._write_catalog(
        "v4.22", {"schema": "olm.template.basic", "entries": entries}
    )
    return repo


def _channel(name: str) -> dict:
    return {"schema": "olm.channel", "package": "mtv-operator", "name": name}


def _default(repo: FBCRepo) -> str:
    with open(f"{repo.tmp_dir.name}/v4.22/catalog-template.json") as f:
        entries = json.load(f)["entries"]
    return next(e for e in entries if e["schema"] == "olm.package")[
        "defaultChannel"
    ]


def test_default_channel_moves_to_newest_y_stream():
    repo = _repo(
        [
            {"schema": "olm.package", "defaultChannel": "release-v2.12"},
            _channel("release-v2.11"),
            _channel("release-v2.12"),
            _channel("release-v5.0"),
        ]
    )
    repo.update_default_channel("v4.22")
    assert _default(repo) == "release-v5.0"


def test_default_channel_compares_versions_numerically_and_skips_others():
    repo = _repo(
        [
            {"schema": "olm.package", "defaultChannel": "release-v2.9"},
            _channel("release-v2.9"),
            _channel("release-v2.10"),
            _channel("dev-preview"),
        ]
    )
    repo.update_default_channel("v4.22")
    assert _default(repo) == "release-v2.10"


def test_default_channel_untouched_without_release_channels():
    repo = _repo(
        [{"schema": "olm.package", "defaultChannel": "release-v2.13"}]
    )
    repo.update_default_channel("v4.22")
    assert _default(repo) == "release-v2.13"
