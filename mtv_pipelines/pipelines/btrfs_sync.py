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


def _deepen(git: Git, branch: str, remote: str) -> None:
    """Fetch *branch* from *remote* with its full history. A plain fetch
    into a shallow repository leaves the shallow boundary in place, so the
    boundary has to be lifted explicitly with --unshallow (git rejects
    --unshallow on a complete repository, hence the check)."""
    if git.is_shallow():
        git.fetch(branch, origin=remote, unshallow=True)
    else:
        git.fetch(branch, origin=remote, depth=None)


def _rebase_onto(
    git: Git,
    branch: str,
    origin: str,
    depth: int | None,
    source_branch: str | None = None,
) -> None:
    """Rebase the current branch onto {origin}/{branch}.

    A shallow fetch can hide the merge-base when the branches diverged by
    more than the depth (e.g. the first sync after the cron was paused for
    a few days): the rebase then replays commits the other side already
    has, conflicting in exactly the files those commits touched. On a
    conflict with a shallow fetch, abort, deepen both sides' history and
    retry once — a conflict at full depth is real divergence and still
    fails.

    *source_branch* is the public branch the checkout came from ("main"
    when *branch* was created from main); it, not *branch*, is the ref
    that needs deepening on the origin side.
    """
    onto = f"{origin}/{branch}"
    try:
        git.rebase(onto)
    except Exception as e:
        if depth is None:
            raise
        logger.warning(
            f"Rebase onto {onto} conflicted ({e}); retrying with a "
            "full-depth fetch — the shallow window likely hid the "
            "merge-base"
        )
        git.rebase_abort()
        _deepen(git, source_branch or branch, "origin")
        if origin != "origin":
            _deepen(git, branch, origin)
        git.rebase(onto)


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
        _rebase_onto(
            git,
            branch,
            "internal",
            fetch_depth,
            source_branch="main" if created_from_main else None,
        )
    else:
        logger.info(f"Branch {branch} not on internal, skipping internal rebase")

    if git.ref_exists(f"origin/{branch}"):
        logger.info(f"Fetching and rebasing onto origin/{branch}")
        git.fetch(branch, origin="origin", depth=fetch_depth)
        _rebase_onto(git, branch, "origin", fetch_depth)
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
