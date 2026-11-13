"""Saturday pipeline: trigger the weekly gate Jenkins jobs for latest IIBs."""

import logging
from argparse import Namespace
from asyncio import TaskGroup

import jenkins
import requests
from config import config
from core.task import depends_on, task
from models.dto import (
    CollectorDTO,
    EmptyDTO,
    JenkinsJobAnalysisDTO,
    JenkinsJobDTO,
    JenkinsJobResultDTO,
    SlackBuildMessageTSDTO,
)
from tasks.latest_iib_state import (
    iter_hyperv_targets,
    iter_tier1_targets,
    load_state,
    mark_hyperv_triggered,
    mark_tier1_triggered,
    mtv_xy_from_version,
)
from utils import iib_short_for_target_ocp
from wrappers.jenkins import JenkinsManager
from wrappers.jenkins_analyzer import JenkinsAnalyzer
from wrappers.slack import Slack

DESCRIPTION = (
    "Trigger Saturday weekly gate Jenkins jobs for the latest recorded IIB."
)

logger = logging.getLogger(__name__)

# Code-side half of the weekly gate wiring: YAML can't hold functions,
# so each gate's target iterator and dedup marker live here, keyed by the
# gate names in config.yaml's weekly_gates map (jobs getter, job suffix,
# and trigger method come from there). Jobs getter and trigger method are
# resolved by name at call time so tests can patch them.
GATE_HELPERS = {
    "tier1": (iter_tier1_targets, mark_tier1_triggered),
    "hyperv": (iter_hyperv_targets, mark_hyperv_triggered),
}


def arg_parse(arg_parser):
    arg_parser.add_argument(
        "-s",
        "--skip-slack",
        help="Skip Slack messages for this Saturday run",
        required=False,
        action="store_true",
    )
    arg_parser.add_argument(
        "--dry-run",
        help="Log jobs that would be triggered without calling Jenkins or Slack",
        required=False,
        action="store_true",
    )


@task
async def trigger_weekly_gate_jobs(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> list[JenkinsJobDTO]:
    path = config.get_latest_iib_state_path()
    state = load_state(path)
    if not state:
        logger.warning(
            f"No latest IIB pointer file at {path}; nothing to trigger"
        )

    results: list[JenkinsJobDTO] = []
    jm = None
    weekly_gates = config.get_weekly_gates()

    # Materialize every gate's config and targets before submitting
    # anything, so a config error (malformed entry or unknown gate
    # included) fails the task before any Jenkins job is triggered.
    gate_targets = {
        gate: list(
            GATE_HELPERS[gate][0](
                getattr(config, gate_cfg["jobs_getter"])(), state
            )
        )
        for gate, gate_cfg in weekly_gates.items()
    }

    for gate, gate_cfg in weekly_gates.items():
        mark_triggered = GATE_HELPERS[gate][1]
        for target in gate_targets[gate]:
            if target.skip_reason:
                logger.info(
                    f"Skipping {gate} for {target.mtv_xy}: "
                    f"{target.skip_reason}"
                )
                continue
            rewritten = iib_short_for_target_ocp(
                target.iib, target.ocp_version
            )
            job_name = (
                f"mtv-{target.mtv_xy}-ocp-{target.ocp_version}-"
                f"{gate_cfg['job_suffix']}"
            )
            if args.dry_run:
                logger.info(
                    f"DRY-RUN would trigger {job_name} "
                    f"MTV={target.mtv_version} IIB={rewritten}"
                )
                continue
            if jm is None:
                jm = JenkinsManager(config.get_jenkins_url())
            try:
                job = await getattr(jm, gate_cfg["trigger_method"])(
                    target.mtv_version, target.ocp_version, rewritten
                )
            except requests.exceptions.ConnectionError as ex:
                logger.error(
                    "Couldn't trigger jenkins CI jobs due to network issues"
                )
                logger.exception(ex)
                return results
            except ValueError as ex:
                logger.error(
                    f"Couldn't trigger {job_name}: no cluster mapping for "
                    f"{target.ocp_version}: {ex}"
                )
                continue
            except jenkins.JenkinsException as ex:
                logger.error(f"Couldn't trigger {job_name}: {ex}")
                logger.exception(ex)
                continue
            if not job:
                logger.warning(
                    f"Did not trigger {job_name} for {target.mtv_version}"
                )
                continue
            mark_triggered(path, target.mtv_xy, target.iib)
            try:
                info = await jm.get_job_info(
                    job["job_name"], job["job_number"]
                )
            except Exception as ex:
                logger.error(
                    f"Couldn't fetch job info for {job['job_name']}/"
                    f"{job['job_number']}; continuing without URL"
                )
                logger.exception(ex)
                info = {}
            results.append(
                JenkinsJobDTO(
                    iib_version=target.mtv_version,
                    job_name=job["job_name"],
                    build_number=job["job_number"],
                    ocp_version=target.ocp_version,
                    job_url=info.get("url", ""),
                )
            )
    return results


@task
@depends_on(trigger_weekly_gate_jobs)
async def send_weekly_gate_slack_triggered(
    data: list[JenkinsJobDTO], args: Namespace, tg: TaskGroup
) -> list[SlackBuildMessageTSDTO]:
    if args.skip_slack or args.dry_run:
        logger.info("Skipping Slack messages for triggered weekly gate jobs")
        return []
    if not data:
        logger.warning("Previous task didn't return any Jenkins jobs")
        return []

    state = load_state(config.get_latest_iib_state_path())
    by_version: dict[str, list[JenkinsJobDTO]] = {}
    for job in data:
        by_version.setdefault(job.iib_version, []).append(job)

    timestamps: list[SlackBuildMessageTSDTO] = []
    slack = Slack()
    for mtv_version, jobs in by_version.items():
        mtv_xy = mtv_xy_from_version(mtv_version)
        entry = state.get(mtv_xy, {})
        ocp = jobs[0].ocp_version
        iib = iib_short_for_target_ocp(entry.get("iib", ""), ocp)
        ts = slack.send_weekly_gates_run(mtv_version, ocp, iib)
        slack.send_triggered_jobs(jobs, ts)
        timestamps.append(ts)
    return timestamps


@task
@depends_on(trigger_weekly_gate_jobs)
async def wait_for_weekly_gate_jenkins_jobs(
    data: list[JenkinsJobDTO], args: Namespace, tg: TaskGroup
) -> list[JenkinsJobResultDTO]:
    if not data:
        logger.warning("Previous task didn't return any Jenkins jobs")
        return []

    results = []
    tasks = []

    async def wait(job: JenkinsJobDTO) -> JenkinsJobResultDTO:
        result = await jm.wait_for_completion(job.job_name, job.build_number)
        url = result.get("url", "")
        status = result.get("result", "")
        return JenkinsJobResultDTO(job=job, result=status, url=url)

    try:
        jm = JenkinsManager(config.get_jenkins_url())
        for job in data:
            tasks.append(tg.create_task(wait(job)))
        for task in tasks:
            results.append(await task)
    except requests.exceptions.ConnectionError as ex:
        logger.error("Couldn't wait for jenkins CI jobs due to network issues")
        logger.exception(ex)
        return []
    return results


@task
@depends_on(wait_for_weekly_gate_jenkins_jobs)
async def analyze_weekly_gate_jobs(
    data: list[JenkinsJobResultDTO], args: Namespace, tg: TaskGroup
) -> list[JenkinsJobAnalysisDTO]:
    if not data:
        logger.warning("Previous task didn't return any Jenkins jobs")
        return []

    results = []
    for job in data:
        ja = JenkinsAnalyzer()
        try:
            results.append(ja.analyze_job(job))
        except requests.RequestException as ex:
            logger.error(
                f"Analyzer failed for {job.url}, posting status without report"
            )
            logger.exception(ex)
            results.append(
                JenkinsJobAnalysisDTO(
                    job_result=job,
                    summary="",
                    child_jobs=[],
                    html_report_url="",
                )
            )
    return results


@task
@depends_on(analyze_weekly_gate_jobs, send_weekly_gate_slack_triggered)
async def send_weekly_gate_slack_ci(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> EmptyDTO:
    if args.skip_slack or args.dry_run:
        logger.info("Skipping Slack CI status for weekly gate jobs")
        return EmptyDTO()
    if not data:
        logger.warning(
            "Previous task didn't return Jenkins jobs or Slack timestamps"
        )
        return EmptyDTO()

    analyses = data.task_outputs.get(analyze_weekly_gate_jobs.name) or []
    timestamps = data.task_outputs.get(send_weekly_gate_slack_triggered.name) or []
    if not analyses:
        logger.warning("Previous task didn't return any Jenkins job results")
        return EmptyDTO()
    if not timestamps:
        logger.warning(
            "Previous task didn't return any slack build message timestamps"
        )
        return EmptyDTO()

    ts_ver_map: dict[str, list[JenkinsJobAnalysisDTO]] = {}
    for job in analyses:
        j_ver = job.job_result.job.iib_version
        ts_ver_map.setdefault(j_ver, []).append(job)

    slack = Slack()
    for ts in timestamps:
        job_analyses = ts_ver_map.get(ts.iib_version, [])
        if not job_analyses:
            continue
        slack.send_ci_status(job_analyses, ts)
    return EmptyDTO()
