import logging
import re
import shutil
import tempfile
from typing import TYPE_CHECKING

from config.config import (
    get_allowed_project_keys,
    get_cmp_mappings,
    get_dev_preview_namespace,
    get_mtv_versions,
    get_release_namespace,
)
from models.iib import IIB
from semver import Version

if TYPE_CHECKING:
    from models.dto import JenkinsJobDTO

PRETTY_PRINT = "  "

logger = logging.getLogger(__name__)

_FBC_PROD_OCP_INFIX = re.compile(r"(forklift-fbc-prod-)v\d+")


def iib_short_for_target_ocp(iib_short: str, ocp_version: str) -> str:
    """Rewrite ``forklift-fbc-prod-v###`` to match *ocp_version*; keep tag/digest unchanged."""
    stripped = ocp_version.lstrip("v")
    compact = f"v{stripped.replace('.', '')}"
    replaced, n = _FBC_PROD_OCP_INFIX.subn(rf"\g<1>{compact}", iib_short, count=1)
    return replaced if n else iib_short
_temp_dirs: list[str] = []


def create_temp_dir(suffix: str = "") -> str:
    if suffix:
        suffix = f"_{suffix}"
    path = tempfile.mkdtemp(suffix=suffix)
    _temp_dirs.append(path)
    return path


def cleanup_temp_dirs():
    for path in _temp_dirs:
        shutil.rmtree(path, ignore_errors=True)
        logger.debug("Cleaned up temp dir: %s", path)
    _temp_dirs.clear()


def replace_for_quay(image: str, version: Version) -> str:
    logger.info(f"Replacing img {image} for quay variant")
    cmp_mappings = get_cmp_mappings()

    # If image is not from official repo, exit
    if "redhat.io" not in image:
        logger.info(f"Couldn't replace {image} for quay. Not from RH.")
        return image

    sha = ""
    tag = ""
    ver = ""
    operator = "forklift-operator"
    # Choose one of the possible suffixes
    if "@sha" in image:
        logger.debug("Using image digest")
        cmp_url, sha = image.split("@sha256:")
    elif ":" in image:
        logger.debug("Using image tag")
        cmp_url, tag = image.split(":")
    else:
        logger.debug("Using just the image")
        cmp_url = image

    # split the original
    registry, namespace, cmp = cmp_url.split("/")

    # Hack for internal konflux cluster
    if cmp == "mtv-virt-v2v-rhel10":
        registry = "quay.io/redhat-user-workloads/rh-mtv-btrfs-tenant"
        ver = "int-"
    else:
        registry = "quay.io/redhat-user-workloads/rh-mtv-1-tenant"

    # Check for type
    if namespace == get_release_namespace():
        ver += f"{version.major}-{version.minor}"
        logger.debug(f"Using versioned component {ver}")
    elif namespace == get_dev_preview_namespace():
        ver += "dev-preview"
        logger.debug(f"Using dev-preview component {ver}")

    new_url = f"{registry}/{operator}-{ver}/"
    new_url += f"{cmp_mappings[cmp].get("upstream")}-{ver}"

    if sha:
        new_url += f"@sha256:{sha}"
    elif tag:
        new_url += f":{tag}"

    logger.info(f"Constructed new image URL {new_url}")

    return new_url


# Figures out what type of build is associated with the version
def parse_version(fbc, ver: Version):
    # If current Z-stream version is greater than 0
    # we can take previous GA Z-stream version, e.g. 2.10.1 -> 2.10.0 GA
    if ver.patch > 0:
        v = Version(ver.major, ver.minor, ver.patch - 1)
        logger.info(f"Using previous GA Z-stream {v}")
        fbc.previous_iib_version = v
        # As all OCP versions point to the same bundle, one is enough
        iib_url = "registry.redhat.io/redhat/redhat-operator-index:"
        iib_url += fbc.for_bundle.ocps[0]
        fbc.previous_iib = IIB(iib_url, v)
    else:
        # New dev-preview version that has no previous build for diff
        # New Y-stream version that doesn't make sense to compare to prev
        logger.info(
            f"Version {ver} doesn't have any good candidates for prev build"
        )


# Extracts jira keys from commit messages (or any other text)
def extract_jira_keys(text: str) -> list[str]:
    text_stripped = text.strip()

    # handle the "None" case immediately
    if re.search(r"(?i)resolves:\s*none", text_stripped):
        return []

    # look for the "Resolves" section (highest priority)
    resolve_match = re.search(
        r"(?i)resolves:\s*(.*)", text_stripped, re.DOTALL
    )

    if resolve_match:
        # If "Resolves:" exists, we parse that section regardless of chore tags
        search_area = resolve_match.group(1)
    else:
        # If no "Resolves:" header, check for chore skip
        if re.match(r"(?i)^chore\(.*?\)", text_stripped):
            return []

        # only look at the part BEFORE the pipe
        search_area = text_stripped.split("|", 1)[0]

    pattern = r"\b([a-z]+-\d+)\b"
    matches = re.findall(pattern, search_area, flags=re.IGNORECASE)

    # fallback, check if key is in allowed projects
    allowed_keys = get_allowed_project_keys()
    keys = []
    for key in matches:
        if key.upper().split("-")[0] in allowed_keys:
            keys.append(key.upper())

    return list(set(keys))


def forklift_branch_from_jenkins_job(job: "JenkinsJobDTO") -> str:
    ver = Version.parse(job.iib_version)
    xy = [str(ver.major), str(ver.minor)]
    for branch, branch_ver in get_mtv_versions().items():
        if branch_ver.split(".")[:2] == xy:
            return branch
    raise ValueError(
        f"No forklift branch found for IIB version {job.iib_version}"
    )
