import logging
import re
from argparse import Namespace
from asyncio import TaskGroup, to_thread

import requests
from config import config
from core.task import depends_on, task
from models.dto import (
    EmptyDTO,
    JenkinsJobAnalysisDTO,
    JenkinsJobDTO,
    JenkinsJobResultDTO,
    SlackBuildMessageTSDTO,
)
from wrappers.jenkins import JenkinsManager
from wrappers.jenkins_analyzer import JenkinsAnalyzer
from wrappers.slack import Slack

DESCRIPTION = "Pipeline to analyze jenkins job"

logger = logging.getLogger(__name__)


def arg_parse(arg_parser):
    arg_parser.add_argument(
        "--jobs",
        help='Jenkins job URLs, example: "https://jenkins-csb-mtv-qe-main.dno.corp.redhat.com/job/mtv-2.11-ocp-4.21-test-release-gate/25"',
        nargs="+",
        required=True,
    )

    arg_parser.add_argument(
        "--iib",
        help='MTV IIB Version, example: "2.11.2-3"',
        required=True,
    )

    arg_parser.add_argument(
        "--ts",
        help="Timestamp of a message, if not specified, will not create thread",
    )

    arg_parser.add_argument(
        "-s",
        "--skip-slack",
        help="Tells the pipeline to skip sending the slack message",
        required=False,
        action="store_true",
    )

    arg_parser.add_argument(
        "-c",
        "--slack-channel",
        help="Specifies to which slack channel a message will be sent",
        required=False,
    )


@task
async def wait_and_analyze_jenkins_jobs(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> list[JenkinsJobAnalysisDTO]:
    if not args.jobs:
        logger.warning("No Jenkins jobs were specified")
        return []
    if not args.iib:
        logger.warning("No MTV IIB Version was specified")
        return []

    regex_pattern = r".*/job/(?P<job_name>mtv-(?P<mtv_version>[\d\.]+)-ocp-(?P<ocp_version>[\d\.]+)-[^/]+)/(?P<build_number>\d+)"

    jobs = []
    for job in args.jobs:
        match = re.search(regex_pattern, job)

        if not match:
            logger.error(f"Couldn't extract info from provided job URL: {job}")
            continue
        results = match.groupdict()

        mtv_version = results["mtv_version"]
        ocp_version = results["ocp_version"]
        job_name = results["job_name"]
        build_number = int(results["build_number"])

        job = JenkinsJobDTO(
            iib_version=args.iib,
            job_name=job_name,
            build_number=build_number,
            ocp_version=ocp_version,
        )
        jobs.append(job)

    async def wait(job: JenkinsJobDTO) -> JenkinsJobAnalysisDTO:
        result = await jm.wait_for_completion(job.job_name, job.build_number)
        job_result = JenkinsJobResultDTO(
            job=job,
            result=result.get("result", ""),
            url=result.get("url", ""),
        )
        try:
            return await to_thread(ja.analyze_job, job_result)
        except Exception as ex:
            logger.error(f"Failed to analyze job {job_result.url}: {ex}")
            return JenkinsJobAnalysisDTO(
                job_result=job_result,
                summary="Analyzer failed",
                child_jobs=[],
                html_report_url=job_result.url,
            )

    tasks = []
    results = []
    try:
        jm = JenkinsManager(config.get_jenkins_url())
        ja = JenkinsAnalyzer()
        for job in jobs:
            tasks.append(tg.create_task(wait(job)))
        for task in tasks:
            results.append(await task)

    except requests.exceptions.ConnectionError as ex:
        logger.error("Couldn't trigger jenkins CI jobs due to network issues")
        logger.exception(ex)
        return []

    return results


@task
@depends_on(wait_and_analyze_jenkins_jobs)
async def send_slack_ci_msg(
    data: list[JenkinsJobAnalysisDTO], args: Namespace, tg: TaskGroup
):
    if args.skip_slack:
        logger.info(
            "Skipping sending of slack message as --skip-slack arg was provided"
        )
        return []

    if not data:
        logger.warning("Previous task didn't return any Jenkins job results")
        return []

    if not args.ts:
        logger.warning(
            "No timestamp was provided, will send message directly to channel"
        )
        timestamp = "0"
    else:
        timestamp = args.ts

    ts = SlackBuildMessageTSDTO(iib_version=args.iib, timestamp=timestamp)

    # Send only failed jobs to slack
    if jobs := [d for d in data if d.job_result.result.lower() == "failure"]:
        if args.slack_channel:
            s = Slack(args.slack_channel)
        else:
            s = Slack()
        s.send_ci_status(jobs, ts)
