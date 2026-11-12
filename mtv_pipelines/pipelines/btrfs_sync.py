import argparse
import logging
from argparse import ArgumentParser, Namespace
from asyncio import TaskGroup

from auth.auth import GitlabAuth
from config import config
from core.task import task
from models.dto import BtrfsSyncResultDTO
from utils import create_temp_dir
from wrappers.git import Git

DESCRIPTION = (
    "Mirror a public forklift branch into the internal GitLab repository.\n\n"
    "Clones github.com/kubev2v/forklift, checks out (or creates from main) the\n"
    "given branch, rebases it onto the internal mirror and onto origin, then\n"
    "force-pushes the result to the internal GitLab repo. Uses shallow,\n"
    "targeted fetches to keep network load low."
)

logger = logging.getLogger(__name__)


def arg_parse(arg_parser: ArgumentParser):
    arg_parser.formatter_class = argparse.RawTextHelpFormatter
    arg_parser.add_argument(
        "-b",
        "--branch",
        required=True,
        help="[required] Branch to sync (e.g. main, release-2.12).",
    )
    arg_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="[optional] Do everything except the final force-push to internal.",
    )
    arg_parser.add_argument(
        "--fetch-depth",
        type=int,
        default=None,
        help="[optional] Override btrfs_sync.fetch_depth from config, for when\n"
        "the branches diverge by more commits than the configured depth.\n"
        "0 fetches the full history (no depth limit).",
    )


@task
async def sync_branch(
    data, args: Namespace, tg: TaskGroup
) -> BtrfsSyncResultDTO:
    branch = args.branch

    btrfs = config.get_btrfs_sync()
    public_repo = btrfs["public_repo"]
    # None: not given, use the config default. 0: no --depth at all.
    if args.fetch_depth is None:
        fetch_depth = btrfs["fetch_depth"]
    else:
        fetch_depth = args.fetch_depth or None

    try:
        internal_url = GitlabAuth().authenticated_url(btrfs["internal_repo"])
    except ValueError as e:
        return BtrfsSyncResultDTO(
            branch=branch, skipped=True, skip_reason=str(e)
        )

    tmp_dir = create_temp_dir(f"btrfs-sync-{branch}")
    git = Git(tmp_dir.name)

    logger.info({"msg": "Cloning (shallow)", "repo": public_repo})
    await git.clone(public_repo, depth=fetch_depth, single_branch=False)

    for option, value in btrfs["large_repo_git_config"].items():
        git.config(option, value)
    git.config("user.email", config.get_git_email())
    git.config("user.name", config.get_git_name())

    # Checkout the branch, or create it from main when origin doesn't have it yet
    created_from_main = False
    if git.ref_exists(f"origin/{branch}"):
        logger.info(f"Branch {branch} exists on origin, checking out")
        git.checkout(branch)
    else:
        logger.info(f"Branch {branch} not on origin, creating from main")
        git.checkout("main")
        git.checkout(branch, create=True)
        created_from_main = True

    logger.info("Disabling SSL verification for internal remote")
    git.config("http.sslVerify", "false")

    git.add_remote("internal", internal_url)

    # Targeted, shallow fetch of just this branch; fall back to HTTP/1.1 on failure
    logger.info(f"Fetching branch {branch} from internal")
    try:
        git.fetch(branch, origin="internal", depth=fetch_depth)
    except Exception as e:
        logger.warning(f"Internal fetch failed ({e}); retrying with HTTP/1.1")
        git.config("http.version", "HTTP/1.1")
        git.fetch(branch, origin="internal", depth=fetch_depth)

    if git.ref_exists(f"internal/{branch}"):
        logger.info(f"Rebasing onto internal/{branch}")
        git.rebase(f"internal/{branch}")
    else:
        logger.info(f"Branch {branch} not on internal, skipping internal rebase")

    if git.ref_exists(f"origin/{branch}"):
        logger.info(f"Fetching and rebasing onto origin/{branch}")
        git.fetch(branch, origin="origin", depth=fetch_depth)
        git.rebase(f"origin/{branch}")
    else:
        logger.info("Branch created from main, skipping origin rebase")

    if args.dry_run:
        logger.info(
            f"Dry-run: would force-push {branch} to internal GitLab mirror"
        )
        return BtrfsSyncResultDTO(
            branch=branch,
            created_from_main=created_from_main,
            dry_run=True,
        )

    logger.info(f"Force-pushing {branch} to internal")
    # with-lease (not plain force): internal/{branch} was fetched above, so this
    # rejects the push if someone updated internal in the meantime.
    git.push(branch=branch, remote_name="internal", force="lease")

    return BtrfsSyncResultDTO(
        branch=branch,
        created_from_main=created_from_main,
        pushed=True,
    )
