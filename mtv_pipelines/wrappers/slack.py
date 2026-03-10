import json
import logging
from datetime import UTC, datetime
from typing import Any, Optional

from auth.auth import SlackAuth
from config import config
from models.dto import (
    JenkinsJobAnalysisDTO,
    SlackBuildMessageDTO,
    SlackBuildMessageTSDTO,
)
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.models.blocks.basic_components import PlainTextObject
from slack_sdk.models.blocks.blocks import HeaderBlock, MarkdownBlock

logger = logging.getLogger(__name__)


from slack_sdk.models.blocks import RichTextBlock


class SlackBuilder:
    def __init__(self):
        self._blocks: list[dict[str, Any]] = []

    def build(self) -> list[dict[str, Any]]:
        return self._blocks

    def to_json(self) -> str:
        return json.dumps(self._blocks, indent=2)

    def header(self, text: str) -> "SlackBuilder":
        self._blocks.append(
            {
                "type": "header",
                "text": {"type": "plain_text", "text": text, "emoji": True},
            }
        )
        return self

    def section(
        self,
        text: str,
        fields: Optional[list[str]] = None,
    ) -> "SlackBuilder":
        block = {"type": "section", "text": {"type": "mrkdwn", "text": text}}

        if fields:
            block["fields"] = [
                {"type": "mrkdwn", "text": f} for f in fields[:10]
            ]
        self._blocks.append(block)
        return self

    def divider(self) -> "SlackBuilder":
        self._blocks.append({"type": "divider"})
        return self

    def context(self, elements: list[str]) -> "SlackBuilder":
        self._blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": e} for e in elements],
            }
        )
        return self

    def rich_text(self, elements: list[dict]) -> "SlackBuilder":
        self._blocks.append({"type": "rich_text", "elements": elements})
        return self

    def table(
        self, rows: list[list[dict]], column_settings: list[dict] = []
    ) -> "SlackBuilder":
        self._blocks.append({"type": "table", "rows": rows})
        if column_settings:
            self._blocks[-1]["column_settings"] = column_settings
        return self

    class Table:
        @staticmethod
        def row(elements: list[list[dict]]) -> list[dict]:
            r = []
            for el in elements:
                r.append(
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": el,
                            }
                        ],
                    }
                )
            return r

    class RichText:
        @staticmethod
        def section(elements: list[dict]) -> dict[str, Any]:
            return {"type": "rich_text_section", "elements": elements}

        @staticmethod
        def quote(elements: list[dict]) -> dict[str, Any]:
            return {"type": "rich_text_quote", "elements": elements}

        @staticmethod
        def preformatted(elements: list[dict]) -> dict[str, Any]:
            return {"type": "rich_text_preformatted", "elements": elements}

        @staticmethod
        def text(
            content: str, bold=False, italic=False, strike=False, code=False
        ) -> dict[str, Any]:
            el: dict[str, str | dict] = {"type": "text", "text": content}
            style: dict[str, bool] = {}
            if bold:
                style["bold"] = True
            if italic:
                style["italic"] = True
            if strike:
                style["strike"] = True
            if code:
                style["code"] = True
            if style:
                el["style"] = style
            return el

        @staticmethod
        def link(
            url: str, text: Optional[str] = None, bold=False
        ) -> dict[str, Any]:
            el: dict[str, str | dict] = {"type": "link", "url": url}
            if text:
                el["text"] = text
            if bold:
                el["style"] = {"bold": True}
            return el

        @staticmethod
        def user(user_id: str) -> dict[str, Any]:
            return {"type": "user", "user_id": user_id}


# for convienence
SB = SlackBuilder


class Slack:
    def __init__(self, channel: str = ""):
        if not channel:
            self.channel = config.get_slack_builds_channel()
        else:
            self.channel = channel
        self.client: WebClient = WebClient(token=SlackAuth().token)

    def send_build(self, msg: SlackBuildMessageDTO):
        logger.info(f"Preparing IIB header message")
        # example: IIB 2.11.0-38 | 05.02.2026 12:46 UTC
        dt = datetime.now(UTC).strftime("%d.%m.%Y %H:%M")
        header = SlackBuilder()
        header.header(f"IIB {msg.iib_version} | {dt} UTC")
        header.context(["*Build type*  |  Automatic"])

        logger.info("Preparing IIB details messages")

        logger.debug("Preparing IIB OCP URLs section")
        ocp_versions = SlackBuilder()
        ocp_versions.header("OCP Versions")
        ocp_versions.divider()
        ocp_lines = []
        for ocp, url in msg.ocp_urls.items():
            ocp_lines.extend(
                [
                    SB.RichText.text(ocp, bold=True),
                    SB.RichText.text("  "),
                    SB.RichText.text(url),
                    SB.RichText.text("\n"),
                ]
            )
        ocp_versions.rich_text([SB.RichText.section(ocp_lines)])

        prev_build = None
        diff_sections = []
        if msg.prev_build[0]:
            logger.debug("Preparing IIB previous build section")
            prev_build = SlackBuilder()
            prev_build.header("Changes introduced since previous build")
            prev_build.divider()
            prev_build.context(
                [
                    "*Previous build*",
                    msg.prev_build[0],
                    msg.prev_build[1].split("/")[-1],
                ]
            )

            logger.debug("Preparing IIB diff sections")
            for change in msg.changes:
                repo = change.repo
                rows = [
                    SB.Table.row(
                        [
                            [SB.RichText.text("Jira")],
                            [SB.RichText.text("Commit")],
                            [SB.RichText.text("SHA")],
                        ]
                    )
                ]
                for commit in change.diff:
                    issue_col = []
                    if commit.issues:
                        for issue in commit.issues:
                            issue_url = config.get_jira_url().rstrip("/")
                            issue_url = f"{issue_url}/browse/{issue.upper()}"
                            issue_col.extend(
                                [
                                    SB.RichText.link(
                                        issue_url,
                                        issue,
                                    ),
                                    SB.RichText.text("\n"),
                                ]
                            )
                    else:
                        # double em-dash ⸺
                        issue_col.append(SB.RichText.text("⸺"))

                    commit_sha_entry = SB.RichText.link(
                        f"{config.get_mtv_repositories()[repo]}/commit/{commit.sha}",
                        commit.sha[:7],
                    )

                    # limit the commit message
                    # due to too long konflux commits...
                    commit_msg = commit.msg.split("\n")[0]
                    char_limit = config.get_commit_character_limit()
                    if len(commit_msg) > char_limit:
                        commit_msg = f"{commit_msg[:char_limit]}..."

                    rows.append(
                        SB.Table.row(
                            [
                                issue_col,
                                [SB.RichText.text(commit_msg)],
                                [commit_sha_entry],
                            ]
                        )
                    )
                ds = SB().section(f"*{repo}* changes")
                diff_sections.append(
                    ds.table(
                        rows,
                        [
                            {"align": "center", "is_wrapped": False},
                            {"align": "left", "is_wrapped": True},
                            {"align": "center", "is_wrapped": False},
                        ],
                    )
                )
        else:
            prev_build = SlackBuilder()
            prev_build.header("This is a first Y-stream build. No changes")

        logger.debug("Preparing konflux info message")
        konflux = SB()
        konflux.header("Konflux")
        konflux.divider()
        konflux.rich_text(
            [
                SB.RichText.section(
                    [
                        SB.RichText.text("Bundle URL"),
                    ]
                ),
                SB.RichText.preformatted([SB.RichText.text(msg.bundle_url)]),
            ]
        )

        image_commits = []
        for image, sha in msg.commits.items():
            image_commits.append(SB.RichText.text(f"{image}: {sha}\n"))
        konflux.rich_text(
            [
                SB.RichText.section(
                    [
                        SB.RichText.text("Commits"),
                    ]
                ),
                SB.RichText.preformatted(image_commits),
            ]
        )

        tses = []
        try:
            ts = self.send_block(header.build(), self.channel)
            tses.append(ts)
            tses.append(
                self.send_block(ocp_versions.build(), self.channel, ts)
            )
            tses.append(self.send_block(prev_build.build(), self.channel, ts))
            for ds in diff_sections:
                tses.append(self.send_block(ds.build(), self.channel, ts))
            tses.append(self.send_block(konflux.build(), self.channel, ts))
        except SlackApiError as e:
            logger.error(e.response["error"])
            if tses:
                logger.warning("Deleting previous messages")
                for ts in tses:
                    self.client.chat_delete(channel=self.channel, ts=ts)
            raise e

        logger.info(f"Sent messages timestamps: {tses}")
        return tses[0]

    def send_block(self, blocks: list, channel: str, ts: str = "") -> str:
        if not ts:
            response = self.client.chat_postMessage(
                channel=channel, blocks=blocks
            )
        else:
            response = self.client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                thread_ts=ts,
            )

        r_ts = response.get("ts", "")
        logger.debug(f"Sent message TS: {r_ts}")
        return r_ts

    def _get_ci_status_emoji(self, status: str) -> str:
        if status.lower() == "failure":
            return ":failed:"
        else:
            return ":done-circle-check:"

    def _get_user_tags(self, jobs: list[JenkinsJobAnalysisDTO]) -> dict:
        statuses = [job.job_result.result.lower() == "failure" for job in jobs]
        if any(statuses):
            users = config.get_slack_failure_mentions()
            mentions = ""
            for user in users:
                if "ic-mtv" in user:
                    mentions += f"<!subteam^{user}> "
                else:
                    mentions += f"<@{user}> "
            if not mentions:
                return {}

            return {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"cc {mentions}",
                },
            }
        return {}

    def send_ci_status(
        self,
        jobs: list[JenkinsJobAnalysisDTO],
        timestamp: SlackBuildMessageTSDTO,
    ):
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Jenkins CI Status",
                    "emoji": True,
                },
            },
            {"type": "divider"},
        ]

        user_mentions = self._get_user_tags(jobs)
        if user_mentions:
            blocks.append(user_mentions)

        for job in jobs:
            job_name = job.job_result.job.job_name
            build_number = job.job_result.job.build_number
            job_url = job.job_result.url
            result = job.job_result.result
            result_emoji = self._get_ci_status_emoji(result)
            analysis_url = job.html_report_url
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{result_emoji} <{job_url}|{job_name} #{build_number}>",
                    },
                    "accessory": {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Analysis",
                            "emoji": True,
                        },
                        "url": analysis_url,
                    },
                }
            )

        ts = self.send_block(
            blocks,
            channel=self.channel,
            ts=timestamp.timestamp,
        )
        print(ts)
