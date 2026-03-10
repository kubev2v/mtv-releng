import json
import logging
import re
import subprocess

from config import config

from mtv_pipelines.auth import auth

COMMAND = ["gh"]
AUTOMATION_LABEL = "automation"

logger = logging.getLogger(__name__)


class GHCLI:
    def __init__(self, cwd: str):
        self.cmd = COMMAND.copy()
        self.cwd = cwd

    def __exec__(self):
        logger.info(f"Executing {self.cmd}")
        result = subprocess.run(
            self.cmd,
            capture_output=True,
            cwd=self.cwd,
        )
        try:
            result.check_returncode()
            logger.debug(f"{self.cmd} output: {result.stdout}")
            return result.stdout
        except subprocess.CalledProcessError:
            err = result.stderr.decode("utf-8")
            raise RuntimeError(err)

    def auth(self):
        self.cmd = [
            "echo",
            f"${auth.GITHUB_TOKEN}",
            "|",
            "gh",
            "auth",
            "login",
            "-p",
            "https",
            "-h",
            "github.com",
            "--with-token",
        ]
        self.__exec__()

        self.cmd = ["gh", "auth", "setup-git"]
        self.__exec__()

    # Example: gh pr list --head "2.10.2" --label "automation" --json url
    def list_pr(self, branch: str, label: str = AUTOMATION_LABEL):
        self.cmd.extend(
            [
                "pr",
                "list",
                "--head",
                branch,
                "--label",
                label,
                "--json",
                "url",
            ]
        )
        return json.loads(self.__exec__())

    # Example:
    # gh pr create --title "$version" --base main --body "" --label automation
    def create_pr(
        self,
        title: str,
        label: str = AUTOMATION_LABEL,
        body: str = "",
        target_branch: str = "main",
    ):
        self.cmd.extend(
            [
                "pr",
                "create",
                "--title",
                title,
                "--base",
                target_branch,
                "--body",
                body,
                "--label",
                label,
            ]
        )
        logger.debug(
            f"Creating PR '{title}' with '{body}' targeting '{target_branch}' with label '{label}'"
        )
        output = self.__exec__()

        # https:\/\/github\.com\/kubev2v\/mtv-fbc\/pull/\d*
        pattern = config.get_mtv_fbc_repo().replace("/", r"\/")
        pattern += r"/pull\/(\d*)"
        match = re.search(pattern, str(output))

        if match:
            return match.group(0)
        raise RuntimeError(f"Couldn't extract PR URL from {output}")

    # Example:
    # gh pr checks --json=name,state https://github.com/kubev2v/mtv-fbc/pull/123
    def list_pr_checks(self, pr_url: str):
        self.cmd.extend(["pr", "checks", "--json=name,state", pr_url])
        return json.loads(self.__exec__())

    # Example:
    # gh pr comment $pr_url --body "/retest forklift-fbc-comp-prod-v420-on-pull-request"
    def comment_on_pr(self, pr_url: str, body: str):
        self.cmd.extend(["pr", "comment", pr_url, "--body", body])
        self.__exec__()
