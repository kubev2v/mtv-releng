import os
from dataclasses import dataclass

SLACK_AUTH = "SLACK_AUTH_TOKEN"
JENKINS_USER = "JENKINS_USER"
JENKINS_TOKEN = "JENKINS_TOKEN"
REGISTRY_PROD_USER = "REGISTRY_PROD_USER"
REGISTRY_PROD_TOKEN = "REGISTRY_PROD_TOKEN"
REGISTRY_STAGE_USER = "REGISTRY_STAGE_USER"
REGISTRY_STAGE_TOKEN = "REGISTRY_STAGE_TOKEN"
STORAGE_OFFLOAD_CLUSTER_EDGE112 = "STORAGE_OFFLOAD_CLUSTER_EDGE112"
GITHUB_TOKEN = "GH_TOKEN"
ROOTCOZ_TOKEN = "ROOTCOZ"


@dataclass
class Auth:
    name: str
    value: str = ""

    def __post_init__(self):
        value = os.getenv(self.name)
        if value is None:
            raise ValueError(f"Environment variable '{self.name}' not found.")
        self.value = value


class SlackAuth:
    def __init__(self):
        self.token = Auth(SLACK_AUTH).value


class JenkinsAuth:
    def __init__(self):
        self.user = Auth(JENKINS_USER).value
        self.token = Auth(JENKINS_TOKEN).value


class StorageOffloadClusterAuth:
    """Kubeadmin password for storage-offload Jenkins jobs; env is per-cluster in config."""

    def __init__(self, password_env: str = STORAGE_OFFLOAD_CLUSTER_EDGE112):
        self.passwd = Auth(password_env).value


class RootcozAuth:
    def __init__(self):
        self.token = Auth(ROOTCOZ_TOKEN).value

    @property
    def bearer_header(self) -> str:
        return f"Bearer {self.token}"
