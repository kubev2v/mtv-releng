import logging
import re
from argparse import Namespace
from asyncio import TaskGroup

import requests
from config import config
from core.task import depends_on, task
from models.dto import (
    CollectorDTO,
    EmptyDTO,
    JenkinsJobAnalysisDTO,
    JenkinsJobDTO,
    JenkinsJobResultDTO,
    RepoCommitDTO,
    RepoDiffDTO,
    SlackBuildMessageTSDTO,
)
from models.fbc_repo import FBCRepo
from models.git_repo import GitRepo
from models.iib import IIB
from semver import Version
from tasks.extract_bundle_from_iib import extract_bundle_from_iib
from tasks.extract_info import extract_info
from tasks.get_commit_diff import get_commit_diff
from tasks.get_mtv_versions import get_mtv_versions
from tasks.prepare_slack_build import prepare_slack_build
from tasks.wait_for_pr import wait_for_pr
from utils import parse_version
from wrappers.gh_cli import GHCLI
from wrappers.jenkins import JenkinsManager
from wrappers.jenkins_analyzer import JenkinsAnalyzer
from wrappers.slack import Slack

DESCRIPTION = "Pipeline to process IIB from FBC PR"


logger = logging.getLogger(__name__)


def arg_parse(arg_parser):
    arg_parser.add_argument(
        "--mtv",
        help='MTV version for FBC PR, example: "2.10.5',
        required=True,
    )

    arg_parser.add_argument(
        "--ocps",
        help='OCP versions for the FBC PR, example: "v4.21 v4.20 v4.19"',
        nargs="+",
        required=True,
    )

    arg_parser.add_argument(
        "-j",
        "--skip-jenkins",
        help="Tells the pipeline to skip triggering jenkins jobs",
        required=False,
        action="store_true",
    )

    arg_parser.add_argument(
        "-s",
        "--skip-slack",
        help="Tells the pipeline to skip sending the slack message",
        required=False,
        action="store_true",
    )


@task
async def prepare_fbc_repo(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> FBCRepo:
    fbc_repo = FBCRepo()

    # Prepare FBC repository
    await fbc_repo.init()
    fbc_repo.git.checkout(fbc_repo.target)
    fbc_repo.git.config("user.email", config.get_git_email())
    fbc_repo.git.config("user.name", config.get_git_name())

    # Download OPM tool, offload downloading to threads
    fbc_repo.download_opm()

    # Check if remote branch for MTV version already exists
    remote_branches = fbc_repo.git.remote_branches()
    remote_branch_exists = False
    logger.info(f"Checking if remote branch for MTV {args.mtv} exists")
    for branch in remote_branches:
        if str(args.mtv) in branch:
            remote_branch_exists = True

    # If remote branch exists, pull it
    if remote_branch_exists:
        logger.info(f"Found remote branch for {args.mtv}")
        fbc_repo.git.checkout(branch=str(args.mtv))
        await fbc_repo.git.pull(branch=str(args.mtv))
    # If not, exit
    else:
        raise RuntimeError(f"Didn't find remote branch for {args.version}")
    return fbc_repo


@task
@depends_on(prepare_fbc_repo)
async def process_pull_request(
    data: FBCRepo, args: Namespace, tg: TaskGroup
) -> FBCRepo:
    if not data:
        logger.warning(f"Previous task didn't return any FBC Repositories")
        return
    fbc_repo = data
    ver = args.mtv
    prs = GHCLI(fbc_repo.tmp_dir.name).list_pr(ver)
    if not prs:
        raise RuntimeError(f"No PRs found for {ver}")
    if len(prs) > 1:
        raise RuntimeError(f"Found multiple PRs for {ver}")
    fbc_repo.pr_url = prs[0]["url"]
    logger.info(f"Found PR {prs[0]["url"]} for {ver}")

    repo_commits = fbc_repo.git.log(20)

    pattern = r"^\d+\.\d+\.\d+-\d+$"
    curr_commit = {}
    prev_commit = {}
    for commit in repo_commits:
        if re.match(
            pattern, commit.get("message", "").split("\n")[0]
        ) and ver in commit.get("message", ""):
            if not curr_commit:
                curr_commit = commit
            else:
                prev_commit = commit
                break

    if not curr_commit:
        raise RuntimeError(
            f"Failed to process PR {fbc_repo}, "
            "no commit matched X.Y.Z-P format"
        )

    ocps = args.ocps
    ocps.sort()
    ocps.reverse()

    iib_url = config.get_fbc_component_url()
    iib_url = iib_url.replace("{ocp}", ocps[0].replace(".", ""))
    iib_url = iib_url.replace("{commit}", curr_commit.get("sha", ""))
    curr_iib = IIB(
        iib_url,
        Version.parse(curr_commit.get("message", "").split("\n")[0]),
    )
    fbc_repo.current_iib = curr_iib
    fbc_repo.current_commit = curr_commit
    fbc_repo.current_iib_version = curr_iib.version

    bundle = extract_bundle_from_iib(curr_iib)
    if bundle:
        bundle.inspect()
        bundle.parse_inspection()
        fbc_repo.for_bundle = bundle

    if prev_commit:
        iib_url = config.get_fbc_component_url()
        iib_url = iib_url.replace("{ocp}", ocps[0].replace(".", ""))
        iib_url = iib_url.replace("{commit}", prev_commit.get("sha", ""))
        prev_iib = IIB(
            iib_url,
            Version.parse(prev_commit.get("message", "").split("\n")[0]),
        )
        fbc_repo.previous_iib = prev_iib
        fbc_repo.previous_commit = prev_commit
        fbc_repo.previous_iib_version = prev_iib.version
    else:
        parse_version(fbc_repo, bundle.version)

    fbc_repo = await wait_for_pr(fbc_repo)
    if not fbc_repo:
        raise RuntimeError(f"Failed to process PR {fbc_repo}")
    return fbc_repo


@task
@depends_on(process_pull_request)
async def extract_prev_iib(
    data: FBCRepo, args: Namespace, tg: TaskGroup
) -> IIB | None:
    if not data:
        logger.warning(f"Previous task didn't return any FBCs")
        return

    return data.previous_iib


@task
@depends_on(process_pull_request)
async def extract_next_iib(
    data: FBCRepo, args: Namespace, tg: TaskGroup
) -> IIB | None:
    if not data:
        logger.warning(f"Previous task didn't return any FBCs")
        return

    return data.current_iib


@task
@depends_on(process_pull_request)
async def prepare_cmp_git_repos(
    data: FBCRepo, args: Namespace, tg: TaskGroup
) -> list[GitRepo]:
    if not data:
        logger.warning(f"Previous task didn't return any FBCs")
        return []

    origin_versions = get_mtv_versions()
    mtv_repos = config.get_mtv_repositories()
    result = []

    fbc_repo = data
    git_repos: dict[str, GitRepo] = {}
    xy_version = str(fbc_repo.for_bundle.version).split(".")[:2]

    logger.debug("Cloning MTV repositories")
    for origin, repo_url in mtv_repos.items():
        gr = GitRepo(repo_url, origin, str(fbc_repo.for_bundle.version))
        await gr.init()
        git_repos[origin] = gr

    logger.debug("Checking out branches")
    for origin, branch_ver in origin_versions.items():
        for branch, ver in branch_ver.items():
            if ver.split(".")[:2] == xy_version:
                git_repos[origin].git.fetch(branch)
                git_repos[origin].git.checkout(branch)
                await git_repos[origin].git.pull(branch)
                logger.debug(f"Checked out {branch}")

    result.extend(git_repos.values())
    return result


@task
@depends_on(
    extract_prev_iib,
    prepare_cmp_git_repos,
)
async def extract_commits_prev(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> list[RepoCommitDTO]:
    if not data:
        logger.warning(f"Previous task didn't return any data")
        return []

    iib = data.task_outputs.get(extract_prev_iib.name)
    git_repos = data.task_outputs.get(prepare_cmp_git_repos.name)

    results = []

    if not iib:
        logger.warning(f"Previous task didn't return any IIBs")
        return []

    if not git_repos:
        logger.warning(f"Previous task didn't return any Git Repositories")
        return []

    results.extend(await extract_info(iib, git_repos))

    return results


@task
@depends_on(
    extract_next_iib,
    prepare_cmp_git_repos,
)
async def extract_commits_next(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> list[RepoCommitDTO]:
    if not data:
        logger.warning(f"Previous task didn't return any data")
        return []

    iib = data.task_outputs.get(extract_next_iib.name)
    git_repos = data.task_outputs.get(prepare_cmp_git_repos.name)

    results = []

    if not iib:
        logger.warning(f"Previous task didn't return any IIBs")
        return []

    if not git_repos:
        logger.warning(f"Previous task didn't return any Git Repositories")
        return []

    results.extend(await extract_info(iib, git_repos))

    return results


@task
@depends_on(
    extract_commits_prev,
    extract_commits_next,
    prepare_cmp_git_repos,
)
async def extract_commit_diff(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> list[RepoDiffDTO]:
    if not data:
        logger.warning(f"Previous task didn't return any data")
        return []

    next_commits = data.task_outputs.get(extract_commits_next.name)
    prev_commits = data.task_outputs.get(extract_commits_prev.name)
    git_repos = data.task_outputs.get(prepare_cmp_git_repos.name)
    results = []

    def find_git_repo_for_ver(
        git_repos: list[GitRepo], repo: str, ver: str
    ) -> GitRepo:
        for gr in git_repos:
            if gr.name != repo:
                continue
            if ".".join(gr.version.split(".")[:2]) != ".".join(
                ver.split(".")[:2]
            ):
                continue
            return gr
        raise ValueError(f"No git repo found for {repo} {ver}")

    if not git_repos:
        logger.warning(f"Previous task didn't return any git repositories")
        return []

    if not next_commits:
        logger.warning(f"Previous task didn't return any current commits")
        return []

    if not prev_commits:
        logger.info(f"No previous commits, build is new Y-stream")
        for next_commit in next_commits:
            results.append(
                RepoDiffDTO(
                    repo=next_commit.repo,
                    diff=[],
                    version=next_commit.version,
                )
            )
        return results

    for ncommit in next_commits:
        repo = ncommit.repo
        version = ncommit.version
        commit = ncommit.sha

        for pcommit in prev_commits:
            if pcommit.repo != repo:
                continue
            if ".".join(pcommit.version.split(".")[:2]) != ".".join(
                version.split(".")[:2]
            ):
                continue

            gr = find_git_repo_for_ver(git_repos, repo, version)
            diff = get_commit_diff(pcommit.sha, commit, gr)
            results.append(RepoDiffDTO(repo=repo, diff=diff, version=version))
            break
    return results


@task
@depends_on(extract_commit_diff, process_pull_request)
async def send_slack_build_msg(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> dict[str, str]:
    if not data:
        logger.warning(f"Previous task didn't return any data")
        return {}

    if not data.task_outputs.get(extract_commit_diff.name):
        logger.warning(f"Previous task didn't return any commit diff")
        return {}

    if not data.task_outputs.get(process_pull_request.name):
        logger.warning(f"Previous task didn't return any FBC repos")
        return {}

    if args.skip_slack:
        logger.info(
            "Skipping sending of slack message as --skip-slack arg was provided"
        )
        return {}

    result = {}
    fbc_repo = data.task_outputs[process_pull_request.name]
    b = prepare_slack_build(
        fbc_repo, data.task_outputs[extract_commit_diff.name]
    )
    ts = Slack().send_build(b)
    result[str(fbc_repo.current_iib_version)] = ts
    return result


@task
@depends_on(process_pull_request)
async def trigger_jenkins_jobs(
    data: FBCRepo, args: Namespace, tg: TaskGroup
) -> list[JenkinsJobDTO]:
    if args.skip_jenkins:
        logger.info(
            "Skipping jenkins triggers as --skip-jenkins arg was provided"
        )
        return []
    if not data:
        logger.warning(f"Previous task didn't return any FBC repos")
        return []

    results = []
    try:
        jm = JenkinsManager(config.get_jenkins_url())
        fbc_repo = data
        iib = fbc_repo.current_iib
        if not iib:
            return []
        iib_short = iib.url.split("/")[-1]
        iib_version = str(fbc_repo.current_iib_version)
        ocps = fbc_repo.for_bundle.ocps
        ocps.sort()
        ocps.reverse()
        version = str(fbc_repo.for_bundle.version)

        job = await jm.trigger_release_gate(version, ocps[0], iib_short)
        if job:
            results.append(
                JenkinsJobDTO(
                    iib_version=iib_version,
                    job_name=job["job_name"],
                    build_number=job["job_number"],
                    ocp_version=ocps[0],
                )
            )
        job = await jm.trigger_release_non_gate(version, ocps[1], iib_short)
        if job:
            results.append(
                JenkinsJobDTO(
                    iib_version=iib_version,
                    job_name=job["job_name"],
                    build_number=job["job_number"],
                    ocp_version=ocps[1],
                )
            )
        # Limit to 2.11 on 4.20
        if "2.11" in version:
            job = await jm.trigger_storage_offload(version, iib_short)
            if job:
                results.append(
                    JenkinsJobDTO(
                        iib_version=iib_version,
                        job_name=job["job_name"],
                        build_number=job["job_number"],
                        ocp_version="v4.20",
                    )
                )
    except requests.exceptions.ConnectionError as ex:
        logger.error("Couldn't trigger jenkins CI jobs due to network issues")
        logger.exception(ex)
        return []
    return results


@task
@depends_on(trigger_jenkins_jobs)
async def wait_for_jenkins_jobs(
    data: list[JenkinsJobDTO], args: Namespace, tg: TaskGroup
) -> list[JenkinsJobResultDTO]:
    if not data:
        logger.warning(f"Previous task didn't return any Jenkins jobs")
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
        logger.error("Couldn't trigger jenkins CI jobs due to network issues")
        logger.exception(ex)
        return []
    return results


@task
@depends_on(wait_for_jenkins_jobs)
async def analyze_jobs(
    data: list[JenkinsJobResultDTO], args: Namespace, tg: TaskGroup
) -> list[JenkinsJobAnalysisDTO]:
    if not data:
        logger.warning(f"Previous task didn't return any Jenkins jobs")
        return []

    results = []
    for job in data:
        ja = JenkinsAnalyzer()
        results.append(ja.analyze_job(job))

    return results


@task
@depends_on(analyze_jobs, send_slack_build_msg)
async def send_slack_ci_msg(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
):
    if not data:
        logger.warning(f"Previous task didn't return any Jenkins jobs")
        return []

    if not data.task_outputs.get(analyze_jobs.name):
        logger.warning(f"Previous task didn't return any Jenkins job results")
        return []

    if not data.task_outputs.get(send_slack_build_msg.name):
        logger.warning(
            f"Previous task didn't return any slack build message timestamps"
        )
        return []

    if args.skip_slack:
        logger.info(
            "Skipping sending of slack message as --skip-slack arg was provided"
        )
        return {}

    ts_ver_map: dict[str, list[JenkinsJobAnalysisDTO]] = {}
    jobs: list[JenkinsJobAnalysisDTO] = data.task_outputs[analyze_jobs.name]
    timestamps: list[SlackBuildMessageTSDTO] = data.task_outputs[
        send_slack_build_msg.name
    ]
    for job in jobs:
        j_ver = job.job_result.job.iib_version
        if not ts_ver_map.get(j_ver, []):
            ts_ver_map[j_ver] = [job]

    for ts in timestamps:
        job_analyses = ts_ver_map.get(ts.iib_version, [])
        if not job_analyses:
            continue
        s = Slack()
        s.send_ci_status(jobs, ts)
