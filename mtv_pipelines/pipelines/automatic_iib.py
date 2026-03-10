import logging
from argparse import Namespace
from asyncio import Task, TaskGroup, sleep, to_thread

import requests
from config import config
from core.task import depends_on, task
from models.bundle import Bundle
from models.dto import (
    CollectorDTO,
    EmptyDTO,
    JenkinsJobAnalysisDTO,
    JenkinsJobDTO,
    JenkinsJobResultDTO,
    MTVBranchVersionDTO,
    MTVRepoBranchVersionsDTO,
    MTVVersionsDTO,
    RepoCommitDTO,
    RepoDiffDTO,
    SlackBuildMessageTSDTO,
    VersionDTO,
)
from models.fbc_repo import FBCRepo
from models.git_repo import GitRepo
from models.iib import IIB
from semver import Version
from tasks.extract_info import extract_info
from tasks.get_commit_diff import get_commit_diff
from tasks.get_mtv_versions import get_mtv_versions
from tasks.prepare_slack_build import prepare_slack_build
from tasks.process_fbc_repo import process_fbc_repo
from tasks.wait_for_pr import wait_for_pr
from utils import replace_for_quay
from wrappers.gh_cli import GHCLI
from wrappers.jenkins import JenkinsManager
from wrappers.jenkins_analyzer import JenkinsAnalyzer
from wrappers.skopeo import Skopeo
from wrappers.slack import Slack

DESCRIPTION = "Pipeline to automate building of IIB."

logger = logging.getLogger(__name__)


def arg_parse(arg_parser):
    arg_parser.add_argument(
        "-f",
        "--process-version",
        help='Only process selected version, example: "2.10.1"',
        required=False,
    )
    arg_parser.add_argument(
        "-b",
        "--process-bundle",
        help='Only process selected bundle, example: "quay.io/redhat-user-workloads/rh-mtv-1-tenant/forklift-operator-dev-preview/forklift-operator-bundle-dev-preview@sha256:8105b225ebc98095d291e01374b375f55ad5018ff59707aee1b79278830cd154"',
        required=False,
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
async def skopeo_login_task(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> EmptyDTO:
    try:
        Skopeo().auth()
    except RuntimeError as e:
        logger.error(f"Failed to login to registries: {e}")
        raise e
    return EmptyDTO()


@task
@depends_on(skopeo_login_task)
async def get_mtv_versions_task(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> MTVVersionsDTO:
    vers = get_mtv_versions()
    mtv_repo_versions = []
    for repo, branch_vers in vers.items():
        mtv_branch_versions = []
        for branch, ver in branch_vers.items():
            v = VersionDTO.model_validate(Version.parse(ver))
            bv = MTVBranchVersionDTO(branch=branch, version=v)
            mtv_branch_versions.append(bv)
        rbv = MTVRepoBranchVersionsDTO(
            repo=repo,
            branch_versions=mtv_branch_versions,
        )
        mtv_repo_versions.append(rbv)
    return MTVVersionsDTO(versions=mtv_repo_versions)


@task
@depends_on(get_mtv_versions_task)
async def get_latest_stage_bundles(
    data: MTVVersionsDTO, args: Namespace, tg: TaskGroup
) -> list[Bundle]:
    if not data.versions:
        logger.warning(f"Previous task didn't return any MTV Versions")
        return []

    cutoff = config.get_mtv_cutoff_version()
    processed_vers = []
    bundles: list[Bundle] = []
    tasks: list[Task] = []

    if args.process_bundle:
        b = Bundle(args.process_bundle)
        try:
            b.inspect()
        except RuntimeError as e:
            logger.warning(f"Wasn't able to inspect the bundle {b.url}")
            logger.warning(e)
            return []
        b.parse_inspection()
        if args.quay:
            b.url = replace_for_quay(b.url, b.version)
        bundles.append(b)
        return bundles

    for cmp, branch_vers in [
        (rv.repo, rv.branch_versions) for rv in data.versions
    ]:
        # Bundles are in forklift repo so skip others
        if cmp != "forklift":
            continue
        for branch, ver in [(bv.branch, bv.version) for bv in branch_vers]:
            if ver.to_version() < Version.parse(cutoff):
                logger.info(
                    f"Skipping version {ver.to_version()} for {branch}"
                    f" in {cmp} because cut-off was set to {cutoff}"
                )
                continue
            if args.process_version:
                if args.process_version != ver.to_version():
                    logger.info(
                        f"Skipping version {ver.to_version()} for {branch}"
                        f" in {cmp} because filter was set to {args.process_version}"
                    )
                    continue
            if branch == "main":
                reg = config.get_dev_preview_namespace()
            else:
                reg = config.get_release_namespace()
            url = f"registry.stage.redhat.io/{reg}/mtv-operator-bundle:{ver.to_version()}"

            b = Bundle(url)

            def inspection(b: Bundle):
                try:
                    b.inspect()
                except RuntimeError as e:
                    logger.warning(f"Wasn't able to inspect the bundle {url}")
                    logger.warning(e)
                    return
                b.parse_inspection()
                if args.quay:
                    b.url = replace_for_quay(b.url, b.version)
                bundles.append(b)
                logger.info(f"Found {b.url} for {b.version}")
                processed_vers.append(str(b.version))

            tasks.append(tg.create_task(to_thread(inspection, b)))
    for task in tasks:
        await task
    return bundles


@task
@depends_on(get_latest_stage_bundles)
async def prepare_fbc_repo(
    data: list[Bundle], args: Namespace, tg: TaskGroup
) -> list[FBCRepo]:
    if not data:
        logger.warning(f"Previous task didn't return any Bundles")
        return []

    async def for_each(bundle: Bundle):
        fbc_repo = FBCRepo()
        fbc_repo.for_bundle = bundle

        # Prepare FBC repository
        await fbc_repo.init()
        fbc_repo.git.checkout(fbc_repo.target)
        fbc_repo.git.config("user.email", config.get_git_email())
        fbc_repo.git.config("user.name", config.get_git_name())

        GHCLI(fbc_repo.tmp_dir.name).auth()

        # Download OPM tool, offload downloading to threads
        await tg.create_task(to_thread(fbc_repo.download_opm))

        # Check if remote branch for MTV version already exists
        remote_branches = fbc_repo.git.remote_branches()
        remote_branch_exists = False
        logger.info(
            f"Checking if remote branch for MTV {bundle.version} exists"
        )
        for branch in remote_branches:
            if str(bundle.version) in branch:
                remote_branch_exists = True

        # If remote branch exists, pull it
        if remote_branch_exists:
            logger.info(f"Found remote branch for {bundle.version}")
            fbc_repo.git.checkout(branch=str(bundle.version))
            await fbc_repo.git.pull(branch=str(bundle.version))
        # If not, create a new one
        else:
            logger.info(f"Creating branch for {bundle.version}")
            fbc_repo.git.checkout(branch=str(bundle.version), create=True)

        return fbc_repo

    tasks = []
    for bundle in data:
        tasks.append(tg.create_task(for_each(bundle)))
    results: list[FBCRepo] = []
    for task in tasks:
        results.append(await task)

    return results


@task
@depends_on(prepare_fbc_repo)
async def process_fbc_repos(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
) -> list[FBCRepo]:
    if not data:
        logger.warning(f"Previous task didn't return any FBC Repositories")
        return []

    tasks = []
    for fbcrepo in data:
        tasks.append(tg.create_task(process_fbc_repo(fbcrepo, tg)))
    results = []
    for task in tasks:
        result = await task
        # only add changed repos
        if result:
            results.append(result)
    return results


@task
@depends_on(process_fbc_repos)
async def process_pull_request(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
) -> list[FBCRepo]:
    if not data:
        logger.warning(f"Previous task didn't return any FBC Repositories")
        return []
    for fbc_repo in data:
        ver = str(fbc_repo.for_bundle.version)
        prs = GHCLI(fbc_repo.tmp_dir.name).list_pr(ver)
        if not prs:
            try:
                pr_url = GHCLI(fbc_repo.tmp_dir.name).create_pr(ver)
                fbc_repo.pr_url = pr_url
                logger.info(
                    f"Found PR {pr_url} for {fbc_repo.for_bundle.version}"
                )
            except RuntimeError as e:
                logger.exception(e)
                return []
        else:
            if len(prs) > 1:
                logger.error(f"Found multiple PRs for {ver}, skipping version")
            fbc_repo.pr_url = prs[0]["url"]
            logger.info(
                f"Found PR {prs[0]["url"]} for {fbc_repo.for_bundle.version}"
            )
    return data


@task
@depends_on(process_pull_request)
async def wait_for_prs(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
) -> list[FBCRepo]:
    if not data:
        logger.warning(f"Previous task didn't return any FBC Repositories")
        return []

    # wait for konflux to schedule the checks
    logger.debug(f"Waiting 10s for Konflux to trigger")
    await sleep(10)

    tasks = []
    for fbc_repo in data:
        if not fbc_repo.pr_url:
            logger.error(
                f"FBC for {fbc_repo.for_bundle.version} didn't have PR URL"
            )
            continue
        logger.info(
            f"Waiting for builds in the PR {fbc_repo.pr_url} to finish"
        )
        tasks.append(tg.create_task(wait_for_pr(fbc_repo)))

    results: list[FBCRepo] = []
    for task in tasks:
        result = await task
        # only add FBCs that were successful
        if result:
            results.append(result)
    return results


@task
@depends_on(process_fbc_repos)
async def extract_prev_iib(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
) -> list[IIB]:
    if not data:
        logger.warning(f"Previous task didn't return any FBCs")
        return []

    iibs = []
    for fbc_repo in data:
        if fbc_repo.previous_iib:
            iibs.append(fbc_repo.previous_iib)
    return iibs


@task
@depends_on(wait_for_prs)
async def extract_next_iib(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
) -> list[IIB]:
    if not data:
        logger.warning(f"Previous task didn't return any FBCs")
        return []

    iibs = []
    for fbc_repo in data:
        if fbc_repo.current_iib:
            iibs.append(fbc_repo.current_iib)
    return iibs


@task
@depends_on(process_pull_request)
async def prepare_cmp_git_repos(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
) -> list[GitRepo]:
    if not data:
        logger.warning(f"Previous task didn't return any FBCs")
        return []

    origin_versions = get_mtv_versions()
    mtv_repos = config.get_mtv_repositories()
    result = []

    for fbc_repo in data:
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

    iibs = data.task_outputs.get(extract_prev_iib.name)
    git_repos = data.task_outputs.get(prepare_cmp_git_repos.name)

    results = []

    if not iibs:
        logger.warning(f"Previous task didn't return any IIBs")
        return []

    if not git_repos:
        logger.warning(f"Previous task didn't return any Git Repositories")
        return []

    for iib in iibs:
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

    iibs = data.task_outputs.get(extract_next_iib.name)
    git_repos = data.task_outputs.get(prepare_cmp_git_repos.name)

    results = []

    if not iibs:
        logger.warning(f"Previous task didn't return any IIBs")
        return []

    if not git_repos:
        logger.warning(f"Previous task didn't return any Git Repositories")
        return []

    for iib in iibs:
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
    for r in results:
        print(r.repo)
        for d in r.diff:
            print(f"  {d.sha} {d.date}")
    return results


@task
@depends_on(extract_commit_diff, wait_for_prs)
async def send_slack_build_msg(
    data: CollectorDTO, args: Namespace, tg: TaskGroup
) -> list[SlackBuildMessageTSDTO]:
    if args.skip_slack:
        logger.info(
            "Skipping sending of slack message as --skip-slack arg was provided"
        )
        return []

    if not data:
        logger.warning(f"Previous task didn't return any data")
        return []

    if not data.task_outputs.get(extract_commit_diff.name):
        logger.warning(f"Previous task didn't return any commit diff")
        return []

    if not data.task_outputs.get(wait_for_prs.name):
        logger.warning(f"Previous task didn't return any FBC repos")
        return []

    result = []
    for fbc_repo in data.task_outputs[wait_for_prs.name]:
        b = prepare_slack_build(
            fbc_repo, data.task_outputs[extract_commit_diff.name]
        )
        ts = Slack().send_build(b)
        result.append(
            SlackBuildMessageTSDTO(
                iib_version=str(fbc_repo.current_iib_version), timestamp=ts
            )
        )
    return result


@task
@depends_on(wait_for_prs)
async def trigger_jenkins_jobs(
    data: list[FBCRepo], args: Namespace, tg: TaskGroup
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
        for fbc_repo in data:
            iib = fbc_repo.current_iib
            if not iib:
                continue
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
            job = await jm.trigger_release_non_gate(
                version, ocps[1], iib_short
            )
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
            if "offload" in job.job_name:
                continue
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
        if "offload" in job.job.job_name:
            continue
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
        return []

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
