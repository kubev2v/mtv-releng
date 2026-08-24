import logging

import requests
from auth.auth import JiraFixedInBuildAuth
from config import config

logger = logging.getLogger(__name__)


class JiraFixedInBuild:
    """Notifies the Jira "fixed-in-build" automation webhook that a build shipped.

    The webhook endpoint is public config; the ``X-Automation-Webhook-Token``
    is the secret and comes from the JIRA_FIXED_IN_BUILD_TOKEN env var.

    Deferred refactor (PR #48 review): when a *second* Jira Automation
    integration lands, extract a ``JiraAutomationWebhook`` base owning the
    invariant POST/header/raise_for_status/best-effort plumbing, with this
    class as a thin subclass supplying its own URL + token and payload shape.
    Not generalized to a single "JiraManager" because each Automation rule has
    its own webhook URL and its own X-Automation-Webhook-Token secret (there is
    no blanket credential for triggering automations), so credentials stay
    per-automation. A shared manager would only fit the Jira REST API, which we
    don't use here. Left as one class until automation #2 exists so the
    abstraction boundary is drawn against a real second payload, not a guess.
    """

    def __init__(self):
        self.webhook_url = config.get_jira_fixed_in_build_webhook_url()
        self.token = JiraFixedInBuildAuth().token

    def notify_build(self, issue_keys: list[str], build_version: str) -> bool:
        """Send *issue_keys* + *build_version* to the fixed-in-build webhook.

        Returns True if Atlassian Automation *accepted* the request (HTTP 2xx).
        """
        if not issue_keys:
            logger.info(f"No Jira issues to notify for build {build_version}")
            return False

        try:
            resp = requests.post(
                self.webhook_url,
                headers={
                    "Content-Type": "application/json",
                    "X-Automation-Webhook-Token": self.token,
                },
                json={
                    "issues": issue_keys,
                    "data": {"buildVersion": build_version},
                },
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(
                f"Failed to notify Jira fixed-in-build for {build_version}: {e}"
            )
            return False

        logger.info(
            f"Jira fixed-in-build webhook accepted request for "
            f"{build_version}: {issue_keys}"
        )
        return True
