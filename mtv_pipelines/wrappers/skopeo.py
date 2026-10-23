import json
import logging
import subprocess

from auth.auth import (
    REGISTRY_PROD_TOKEN,
    REGISTRY_PROD_USER,
    REGISTRY_STAGE_TOKEN,
    REGISTRY_STAGE_USER,
)

COMMAND = ["skopeo"]
PROTOCOL = "docker://"

# registry signatures for a genuinely-absent image/tag (NOT auth/network).
# Kept narrow on purpose: a broad "not found" would also match a missing
# "command not found" binary and misclassify it as an absent image.
# "deleted or has expired" is quay's message for a GC'd (time-machine) tag.
_NOT_FOUND_SIGNATURES = (
    "manifest unknown",
    "name unknown",
    "deleted or has expired",
)


logger = logging.getLogger(__name__)


class ImageNotFoundError(RuntimeError):
    """Raised when skopeo reports the image/manifest/tag does not exist."""


class Skopeo:
    def __init__(self):
        self.cmd = COMMAND.copy()

    def __exec__(self):
        logger.info(f"Executing {self.cmd}")

        result = subprocess.run(
            self.cmd,
            capture_output=True,
        )
        try:
            result.check_returncode()
            return result.stdout
        except subprocess.CalledProcessError:
            err = result.stderr.decode("utf-8")
            if any(sig in err.lower() for sig in _NOT_FOUND_SIGNATURES):
                raise ImageNotFoundError(err)
            raise RuntimeError(err)

    def __prepare_url__(self, url: str) -> str:
        if PROTOCOL in url:
            return url
        return PROTOCOL + url

    def __override_arch__(self, arch: str | None) -> None:
        # Pin the platform so multi-arch images resolve the same way on every
        # host (e.g. an arm64 laptop vs. the amd64 releng host).
        if arch:
            self.cmd.extend(["--override-os", "linux", "--override-arch", arch])

    def inspect(self, img_url: str, arch: str | None = None) -> dict:
        self.__override_arch__(arch)
        self.cmd.extend(
            [
                "inspect",
                "--no-tags",
                self.__prepare_url__(img_url),
            ]
        )

        return json.loads(self.__exec__())

    def copy(
        self, img_url: str, target_path: str, arch: str | None = None
    ) -> None:
        self.__override_arch__(arch)
        self.cmd.extend(
            [
                "copy",
                self.__prepare_url__(img_url),
                f"dir://{target_path}",
            ]
        )
        self.__exec__()

    def __login__(self, registry: str, user_var: str, token_var: str) -> None:
        self.cmd = ["bash", "-c"]
        self.cmd.append(f"skopeo login {registry} -u ${user_var} -p ${token_var}")
        self.__exec__()

    def auth(self) -> None:
        self.__login__(
            "registry.stage.redhat.io", REGISTRY_STAGE_USER, REGISTRY_STAGE_TOKEN
        )
        self.auth_prod()

    def auth_prod(self) -> None:
        """Log in to registry.redhat.io only (no stage credentials needed)."""
        self.__login__(
            "registry.redhat.io", REGISTRY_PROD_USER, REGISTRY_PROD_TOKEN
        )

    def inspect_raw(self, img_url: str) -> bytes:
        """Raw manifest (list) bytes; sha256 of them is the pinnable digest."""
        self.cmd.extend(["inspect", "--raw", self.__prepare_url__(img_url)])
        return self.__exec__()

    def list_tags(self, repo: str) -> list[str]:
        """All tags of *repo*, e.g. registry.redhat.io/ubi9/go-toolset."""
        self.cmd.extend(["list-tags", self.__prepare_url__(repo)])
        return json.loads(self.__exec__()).get("Tags", [])
