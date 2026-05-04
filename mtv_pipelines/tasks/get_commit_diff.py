import logging

from models.dto import CommitDTO
from models.git_repo import GitRepo
from utils import extract_jira_keys

logger = logging.getLogger(__name__)


def get_commit_diff(
    prev_commit: str, next_commit: str, git_repo: GitRepo
) -> list[CommitDTO]:
    results = []
    commits = git_repo.git.log(max_count=200)

    if prev_commit not in [commit.get("sha") for commit in commits]:
        logger.error(
            f"Previous commit '{prev_commit}' not found in "
            f"{[commit.get('sha') for commit in commits]}"
        )
        return results

    if next_commit not in [commit.get("sha") for commit in commits]:
        logger.error(
            f"Next commit '{next_commit}' not found in "
            f"{[commit.get('sha') for commit in commits]}"
        )
        return results

    found_start = False
    found_end = False
    for commit in commits:
        if found_start and found_end:
            break
        if next_commit == commit.get("sha"):
            found_start = True
        if prev_commit == commit.get("sha"):
            found_end = True
            continue

        if not found_start:
            continue

        # try to also find jiras
        c = CommitDTO(
            sha=commit.get("sha", ""),
            msg=commit.get("message", ""),
            author=commit.get("author", ""),
            date=commit.get("date", ""),
            issues=extract_jira_keys(commit.get("message", "")),
        )
        results.append(c)

    return results
