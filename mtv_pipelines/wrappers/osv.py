import logging

import requests
from config import config

logger = logging.getLogger(__name__)


class OsvLookupError(RuntimeError):
    """OSV could not be queried (network error, rate limit, server error)."""


def get_vuln(vuln_id: str) -> dict | None:
    """Fetch an OSV record (CVE-*, GHSA-*, GO-*, PYSEC-*).

    Returns None only when OSV confirms the record doesn't exist (404); any
    other failure raises OsvLookupError, so a transient outage isn't mistaken
    for "no record" and triaged on weaker evidence.
    """
    url = f"{config.get_cve_triage()['osv_api']}/{vuln_id}"
    try:
        resp = requests.get(url, timeout=config.get_timeouts()["http_seconds"])
    except requests.RequestException as e:
        raise OsvLookupError(f"OSV lookup for {vuln_id} failed: {e}") from e
    if resp.status_code == 404:
        logger.info(f"OSV has no record for {vuln_id}")
        return None
    if resp.status_code != 200:
        raise OsvLookupError(
            f"OSV lookup for {vuln_id} returned {resp.status_code}"
        )
    return resp.json()
