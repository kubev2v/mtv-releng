import json
import logging
import os
import re
import subprocess

from auth.auth import REGISTRY_PROD_TOKEN, REGISTRY_PROD_USER
from config.config import get_mtv_fbc_repo
from models.bundle import Bundle
from models.iib import IIB
from semver import Version
from utils import create_temp_dir
from wrappers.git import Git

_RELEASE_CHANNEL = re.compile(r"^release-v(\d+)\.(\d+)$")


class FBCRepo:
    def __init__(self, target: str = "main"):
        self.tmp_dir = create_temp_dir("fbc_repo")
        self.url = get_mtv_fbc_repo()
        self.target = target
        self.logger = logging.getLogger(self.tmp_dir.name)
        self.for_bundle: Bundle = Bundle("")
        self.previous_commit: dict = {}
        self.previous_iib_version: Version | None = None
        self.current_commit: dict = {}
        self.current_iib_version: Version | None = None
        self.pr_url: str = ""
        self.previous_iib: IIB | None = None
        self.current_iib: IIB | None = None

    async def init(self):
        self.git = Git(self.tmp_dir.name)
        await self.git.clone(self.url)

    def download_opm(self):
        self.logger.info(f"Downloading OPM into {self.tmp_dir.name}")
        result = subprocess.run(
            ["bash", "-c", "source opm_utils.sh && download_opm_client"],
            capture_output=True,
            cwd=self.tmp_dir.name,
        )
        try:
            result.check_returncode()
            self.logger.info(f"Downloaded OPM into {self.tmp_dir.name}")
        except subprocess.CalledProcessError:
            err = result.stderr.decode("utf-8")
            raise RuntimeError(err)

    def init_catalog(self, catalog: str):
        self.logger.info(f"Initializing catalog {catalog}")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"skopeo login -u ${REGISTRY_PROD_USER} -p ${REGISTRY_PROD_TOKEN} registry.redhat.io && ./generate-fbc.sh --init {catalog}",
            ],
            capture_output=True,
            cwd=self.tmp_dir.name,
        )
        try:
            result.check_returncode()
            self.logger.info(f"Catalog {catalog} initialized")
        except subprocess.CalledProcessError:
            err = result.stderr.decode("utf-8")
            raise RuntimeError(err)

    def update_default_channel(self, catalog: str):
        """Point defaultChannel at the newest Y stream channel
        (release-vX.Y) in the catalog; other channel names are ignored."""
        content = self._read_catalog(catalog)
        streams = []
        for entry in content.get("entries"):
            if entry.get("schema") != "olm.channel":
                continue
            m = _RELEASE_CHANNEL.match(entry.get("name", ""))
            if m:
                streams.append((int(m.group(1)), int(m.group(2))))
        if not streams:
            return
        major, minor = max(streams)
        newest = f"release-v{major}.{minor}"
        for entry in content.get("entries"):
            if entry.get("schema") == "olm.package":
                if entry.get("defaultChannel") != newest:
                    self.logger.info(
                        f"Setting defaultChannel of {catalog} to {newest}"
                        f" (was {entry.get('defaultChannel')})"
                    )
                    entry["defaultChannel"] = newest
                    self._write_catalog(catalog, content)
                return

    def render_catalog(self, catalog: str):
        self.update_default_channel(catalog)
        self.logger.info(f"Rendering catalog {catalog}")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"skopeo login -u ${REGISTRY_PROD_USER} -p ${REGISTRY_PROD_TOKEN} registry.redhat.io && ./generate-fbc.sh --render-template {catalog}",
            ],
            capture_output=True,
            cwd=self.tmp_dir.name,
        )
        try:
            result.check_returncode()
            self.logger.info(f"Catalog {catalog} rendered")
        except subprocess.CalledProcessError:
            err = result.stderr.decode("utf-8")
            raise RuntimeError(err)

    def has_catalog(self, catalog: str):
        self.logger.info(f"Checking if catalog {catalog} exists")
        return catalog in os.listdir(self.tmp_dir.name)

    def catalog_has_bundle(self, catalog: str):
        entries = self._read_catalog(catalog).get("entries")
        for entry in entries:
            if entry.get("schema") == "olm.bundle":
                if self.for_bundle.digest in entry.get("image"):
                    return True
        return False

    def catalog_has_channel(self, catalog: str, channel: str):
        entries = self._read_catalog(catalog).get("entries")
        for entry in entries:
            if entry.get("schema") == "olm.channel":
                if entry.get("name") == channel:
                    return True
        return False

    def catalog_has_version_entry(
        self, catalog: str, channel: str, ver_entry: dict
    ) -> bool:
        self.logger.debug(
            f"Checking if catalog {catalog} has version entry"
            f" {ver_entry} in channel {channel}"
        )
        # If channel is missing, raise an error
        if not self.catalog_has_channel(catalog, channel):
            raise RuntimeError(
                f"Catalog {catalog} doesn't have channel {channel}"
            )
        content = self._read_catalog(catalog)
        entries = content.get("entries")
        old_ver_entries = None
        for entry in entries:
            if entry.get("schema") == "olm.channel":
                if entry.get("name") == channel:
                    old_ver_entries = entry.get("entries")
        if not old_ver_entries:
            self.logger.debug(
                "Couldn't find any version entries for channel "
                f"{channel} in catalog {catalog}"
            )
            return False

        # Check for already existing version entry
        for version in old_ver_entries:
            if version.get("name") == ver_entry.get("name"):
                return True
        return False

    def add_entry_to_catalog(self, catalog: str, entry: dict):
        self.logger.debug(f"Adding entry {entry} to catalog {catalog}")
        content = self._read_catalog(catalog)
        content.get("entries").append(entry)
        self._write_catalog(catalog, content)
        self.logger.debug(f"Entry {entry} added to catalog {catalog}")

    def add_version_entry_to_channel(
        self, catalog: str, channel: str, ver_entry: dict
    ):
        self.logger.debug(
            f"Adding version entry {ver_entry} to channel {channel} in {catalog}"
        )
        content = self._read_catalog(catalog)
        entries = content.get("entries")
        old_ver_entries = None
        channel_idx = 0
        for entry in entries:
            if entry.get("schema") == "olm.channel":
                if entry.get("name") == channel:
                    old_ver_entries = entry.get("entries")
                    break
            channel_idx += 1
        if not old_ver_entries:
            self.logger.error(
                "Couldn't find version entries for channel "
                f"{channel} in catalog {catalog}"
            )
            raise RuntimeError("Failed to find version entry in catalog")

        # Check for already existing version entry
        # If found, check for matching bundle entry, if found exit, if not add it
        for version in old_ver_entries:
            if version.get("name") == ver_entry.get("name"):
                bundle_entries = self._get_bundles(catalog)
                # TODO: Inspection is sequential and could take a long time
                # depending on the number of bundles
                for bundle_entry in bundle_entries:
                    img = bundle_entry.get("image", "")
                    if not img:
                        raise RuntimeError(
                            "Failed to get 'image' from bundle entry"
                        )
                    b = Bundle(img)
                    b.inspect()
                    if b.version == self.for_bundle.version:
                        self.logger.info(
                            f"Bundle entry for {b.version} already present in {catalog}"
                        )
                        return
        content.get("entries")[channel_idx].get("entries").append(ver_entry)
        self._write_catalog(catalog, content)

        self.logger.debug(
            f"Added version entry {ver_entry} to channel {channel} in {catalog}"
        )

    def _get_bundles(self, catalog: str) -> list[dict]:
        self.logger.debug(f"Getting bundles from catalog {catalog}")
        content = self._read_catalog(catalog)
        entries = content.get("entries")
        bundle_entries = []
        for entry in entries:
            if entry.get("schema") == "olm.bundle":
                bundle_entries.append(entry)
        return bundle_entries

    def _read_catalog(self, catalog: str):
        self.logger.debug(f"Reading catalog {catalog}")
        with open(
            f"{self.tmp_dir.name}/{catalog}/catalog-template.json", "r"
        ) as file:
            content = json.loads(file.read())
        self.logger.debug(f"Content from catalog {catalog}: {content}")
        return content

    def _write_catalog(self, catalog: str, content: dict):
        self.logger.debug(f"Writing {content} to catalog {catalog}")
        with open(
            f"{self.tmp_dir.name}/{catalog}/catalog-template.json", "w"
        ) as file:
            file.write(json.dumps(content, indent=4))
        self.logger.debug(f"Wrote {content} to catalog {catalog}")

    def __str__(self):
        s = "FBC Repo:\n"
        s += f"  Location: {self.tmp_dir}\n"
        s += f"  URL: {self.url}\n"
        s += f"  Target: {self.target}\n"
        s += f"  Previous commit: {self.previous_commit}\n"
        s += f"  Previous IIB: {self.previous_iib}\n"
        s += f"  Previous IIB version: {self.previous_iib_version}\n"
        s += f"  Current commit: {self.current_commit}\n"
        s += f"  Current IIB: {self.current_iib}\n"
        s += f"  Current IIB version: {self.current_iib_version}"
        return s
