import asyncio
import logging
from typing import Any, Dict, List

import git

logger = logging.getLogger(__name__)


def _redact(url: str) -> str:
    """Strip any user:token@ userinfo from a URL so creds never reach the logs.

    Parses rather than regexes: userinfo only lives in the netloc (before the
    first "/"), so stripping everything up to the last "@" there handles tokens
    with any characters (e.g. an unencoded "/" or "@") without touching the path.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    netloc, slash, path = rest.partition("/")
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return f"{scheme}{sep}{netloc}{slash}{path}"


class Git:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    async def clone(
        self,
        url: str,
        depth: int | None = None,
        single_branch: bool | None = None,
        branch: str | None = None,
    ) -> None:
        logger.info(f"Cloning from {_redact(url)} to {self.repo_path}")
        kwargs: dict[str, Any] = {}
        if depth is not None:
            kwargs["depth"] = depth
        if branch is not None:
            kwargs["branch"] = branch
        if single_branch is False:
            kwargs["no_single_branch"] = True
        self.repo = await asyncio.to_thread(
            lambda: git.Repo.clone_from(url, self.repo_path, **kwargs)
        )
        logger.info(f"Finished cloning from {_redact(url)} to {self.repo_path}")

    def add_remote(self, name: str, url: str) -> None:
        self._ensure_repo()
        logger.info(f"Adding remote '{name}' -> {_redact(url)}")
        self.repo.create_remote(name, url)

    def ref_exists(self, ref: str) -> bool:
        self._ensure_repo()
        try:
            self.repo.git.rev_parse("--verify", ref)
            return True
        except git.GitCommandError:
            return False

    def rebase(self, upstream: str) -> None:
        self._ensure_repo()
        logger.info(f"Rebasing onto {upstream}")
        self.repo.git.rebase(upstream)

    def rebase_abort(self) -> None:
        self._ensure_repo()
        self.repo.git.rebase("--abort")

    def merge(self, ref: str, message: str | None = None) -> None:
        self._ensure_repo()
        logger.info(f"Merging {ref}")
        args = [ref]
        if message:
            args.extend(["-m", message])
        self.repo.git.merge(*args)

    def merge_abort(self) -> None:
        self._ensure_repo()
        self.repo.git.merge("--abort")

    async def pull(
        self, branch: str = "main", remote_name: str = "origin"
    ) -> None:
        self._ensure_repo()
        try:
            remote = self.repo.remote(name=remote_name)
            logger.info(f"Pulling from {str(remote)}/{branch}...")
            await asyncio.to_thread(remote.pull, branch)
            logger.info("Pull successful")
        except ValueError:
            raise RuntimeError(f"Remote {remote_name} not found")
        except git.GitCommandError as e:
            raise RuntimeError(f"Error pulling changes: {e}")

    def push(
        self,
        branch: str = "main",
        remote_name: str = "origin",
        push_options: list[str] | None = None,
        force: str | None = None,
    ) -> None:
        self._ensure_repo()
        try:
            args = [remote_name, f"{branch}:{branch}"]
            if force == "lease":
                args.insert(0, "--force-with-lease")
            elif force:
                args.insert(0, "-f")
            for opt in push_options or []:
                args.extend(["-o", opt])
            logger.info(f"Pushing to {remote_name}/{branch}...")
            self.repo.git.push(*args)
            logger.info("Push successful")
        except git.GitCommandError as e:
            raise RuntimeError(f"Error pushing changes: {e}")

    def log(self, max_count: int = 10) -> List[Dict[str, Any]]:
        self._ensure_repo()
        logs = []
        for commit in self.repo.iter_commits(max_count=max_count):
            logs.append(
                {
                    "sha": commit.hexsha,
                    "author": commit.author.name,
                    "date": commit.committed_datetime.isoformat(),
                    "message": commit.message.strip(),
                }
            )
        return logs

    def branches(self) -> List[str]:
        self._ensure_repo()
        return [head.name for head in self.repo.heads]

    def remote_branches(self) -> List[str]:
        self._ensure_repo()
        branches = []
        for remote in self.repo.remotes:
            for ref in remote.refs:
                branches.append(ref.name)
        return branches

    def checkout(self, branch: str, create: bool = False) -> None:
        self._ensure_repo()
        if create:
            logger.info(f"Creating and checking out new branch {branch}")
            self.repo.git.checkout("-b", branch)
        else:
            logger.info(f"Checking out {branch}")
            self.repo.git.checkout(branch)

        if not self.repo.head.is_detached:
            logger.info(f"Switched to branch: {self.repo.active_branch.name}")
        else:
            logger.info(
                f"HEAD is now detached at {self.repo.head.commit.hexsha[:7]}"
            )

    def fetch(
        self, branch: str, origin: str = "origin", depth: int | None = None
    ) -> None:
        self._ensure_repo()
        logger.info(f"Fetching branch '{branch}' from origin '{origin}'")
        args = [origin, branch]
        if depth is not None:
            args = [f"--depth={depth}", *args]
        self.repo.git.fetch(*args)

    def config(self, option: str, value: str):
        self._ensure_repo()
        self.repo.git.config(option, value)

    def has_changes(self):
        logger.debug(
            f"Repo {self.repo_path} has these changes "
            f"{self.repo.git.execute(["git", "--no-pager", "diff"])}"
        )
        return self.repo.is_dirty(untracked_files=True)

    def add_files(self, files: list[str]):
        self.repo.index.add(files)

    def add_paths(self, paths: list[str]):
        # Uses `git add` (not index.add) so directories and deletions/renames
        # within a pathspec get staged, e.g. renamed .tekton/ files.
        self.repo.git.add(*paths)

    def restore_paths(self, paths: list[str]):
        """Discard tracked and untracked changes under *paths*."""
        self.repo.git.checkout("HEAD", "--", *paths)
        self.repo.git.clean("-fdq", "--", *paths)

    def changed_paths(self) -> list[str]:
        """Paths with uncommitted changes, including untracked files."""
        out = self.repo.git.status("--porcelain", "--untracked-files=all")
        return [line[3:] for line in out.splitlines() if line]

    def commit(self, message: str):
        self.repo.git.commit("-s", "-m", message)

    def _ensure_repo(self):
        if self.repo is None:
            raise RuntimeError(
                "Repository not initialized. Run clone() or ensure path is valid."
            )
