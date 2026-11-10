import os
from dataclasses import dataclass
from urllib.parse import quote

SLACK_AUTH = "SLACK_AUTH_TOKEN"
JENKINS_USER = "JENKINS_USER"
JENKINS_TOKEN = "JENKINS_TOKEN"
REGISTRY_PROD_USER = "REGISTRY_PROD_USER"
REGISTRY_PROD_TOKEN = "REGISTRY_PROD_TOKEN"
REGISTRY_STAGE_USER = "REGISTRY_STAGE_USER"
REGISTRY_STAGE_TOKEN = "REGISTRY_STAGE_TOKEN"
STORAGE_OFFLOAD_CLUSTER_EDGE112 = "STORAGE_OFFLOAD_CLUSTER_EDGE112"
GITHUB_TOKEN = "GH_TOKEN"
GITLAB_TOKEN = "GITLAB_TOKEN"
GITLAB_USER = "GITLAB_USER"
ROOTCOZ_TOKEN = "ROOTCOZ"
JIRA_FIXED_IN_BUILD_TOKEN = "JIRA_FIXED_IN_BUILD_TOKEN"
JIRA_EMAIL = "JIRA_EMAIL"
JIRA_API_TOKEN = "JIRA_API_TOKEN"


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


class GitlabAuth:
    """Token auth for the internal GitLab mirror.

    GITLAB_TOKEN is required; GITLAB_USER is optional (GitLab accepts the
    "oauth2" username with a token when no user is set).
    """

    def __init__(self):
        self.token = Auth(GITLAB_TOKEN).value
        self.user = os.getenv(GITLAB_USER) or ""

    def authenticated_url(self, url: str) -> str:
        prefix = "https://"
        if not url.startswith(prefix):
            raise ValueError(f"Expected an https:// GitLab URL, got: {url}")
        # Percent-encode so reserved chars (e.g. a "/" in the token) can't break
        # the URL or defeat Git._redact's credential stripping in logs.
        user = quote(self.user or "oauth2", safe="")
        token = quote(self.token, safe="")
        return f"{prefix}{user}:{token}@{url[len(prefix):]}"


class RootcozAuth:
    def __init__(self):
        self.token = Auth(ROOTCOZ_TOKEN).value

    @property
    def bearer_header(self) -> str:
        return f"Bearer {self.token}"


class JiraFixedInBuildAuth:
    def __init__(self):
        self.token = Auth(JIRA_FIXED_IN_BUILD_TOKEN).value


class JiraAuth:
    """Basic auth (account email + API token) for the Jira Cloud REST API."""

    def __init__(self):
        self.email = Auth(JIRA_EMAIL).value
        self.token = Auth(JIRA_API_TOKEN).value
