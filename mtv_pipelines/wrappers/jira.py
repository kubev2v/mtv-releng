import logging

import requests
from auth.auth import JiraAuth
from config import config

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 100


class JiraForbiddenError(RuntimeError):
    """403 from Jira, e.g. an embargoed security issue the account can't see."""


class Jira:
    """Minimal Jira Cloud REST client (search, comments, labels).

    Unlike JiraFixedInBuild (an Automation webhook), this talks to the REST API
    with JIRA_EMAIL + JIRA_API_TOKEN basic auth. Comments go through the v2
    endpoint so the body can be plain wiki markup instead of ADF.
    """

    def __init__(self):
        jira_auth = JiraAuth()
        self.base_url = config.get_jira_url().rstrip("/")
        self.timeout = config.get_timeouts()["http_seconds"]
        self.session = requests.Session()
        self.session.auth = (jira_auth.email, jira_auth.token)
        self.session.headers["Accept"] = "application/json"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        resp = self.session.request(
            method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
        )
        if resp.status_code == 403:
            raise JiraForbiddenError(f"{method} {path}: {resp.text}")
        resp.raise_for_status()
        return resp

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        issues: list[dict] = []
        token = None
        while True:
            body = {
                "jql": jql,
                "fields": fields,
                "maxResults": SEARCH_PAGE_SIZE,
            }
            if token:
                body["nextPageToken"] = token
            data = self._request(
                "POST", "/rest/api/3/search/jql", json=body
            ).json()
            issues.extend(data.get("issues", []))
            token = data.get("nextPageToken")
            # Keep paging while Jira hands out a token; a missing isLast must
            # not end the search early (pages can be shorter than maxResults).
            if not token or data.get("isLast") is True:
                return issues

    def get_comments(self, key: str) -> list[str]:
        data = self._request("GET", f"/rest/api/2/issue/{key}/comment").json()
        return [c.get("body", "") for c in data.get("comments", [])]

    def add_comment(self, key: str, body: str) -> None:
        self._request(
            "POST", f"/rest/api/2/issue/{key}/comment", json={"body": body}
        )
        logger.info(f"Commented on {key}")

    def add_labels(self, key: str, labels: list[str]) -> None:
        self._request(
            "PUT",
            f"/rest/api/3/issue/{key}",
            json={"update": {"labels": [{"add": label} for label in labels]}},
        )
        logger.info(f"Added labels {labels} to {key}")
