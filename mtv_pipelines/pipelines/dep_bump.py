import argparse
import datetime
import hashlib
import logging
import os
import re
from argparse import ArgumentParser, Namespace
from asyncio import TaskGroup
from dataclasses import replace

import git as gitpython
from config import config
from core.task import task
from models.dto import (
    DepBumpRepoResultDTO,
    DepBumpResultDTO,
    EmptyDTO,
    GoBumpDTO,
    ImageBumpDTO,
    RejectedBumpDTO,
)
from tasks.dep_bump import (
    Candidate,
    ModFile,
    cascades,
    containerfiles,
    go_toolset_for,
    go_toolset_version,
    newest_in_stream,
    newest_ocp_tag,
    ocp_stream,
    parse_from_lines,
    parse_mod_json,
    patch_candidates,
    render_body,
    render_commit,
    rewrite_ref,
    rpm_components,
    security_candidates,
    semver,
    side_bumps,
    split_ref,
)
from utils import create_temp_dir
from wrappers.gh_cli import GHCLI
from wrappers.git import Git
from wrappers.go import Go, GoCommandError
from wrappers.skopeo import Skopeo

DESCRIPTION = (
    "Mass dependency + base image update for a release branch.\n\n"
    "Refreshes downstream Containerfile base images to the newest build of\n"
    "the same version stream, and applies Go security fixes (govulncheck)\n"
    "and patch bumps of direct dependencies. Bumps that cascade into guarded\n"
    "ecosystems (k8s, kubevirt, ...), need a go directive raise the module's\n"
    "builders can't take, or break the build are left out and reported.\n"
    "RPM lockfiles are not touched; components that need a lockfile refresh\n"
    "are listed in the PR with the scripts/rpm_lockfile_refresh.sh command.\n"
    "One PR per repo per branch. Dry-run by default; pass --apply to push."
)

logger = logging.getLogger(__name__)

# go: golang.org/x/text@v0.41.0 indirectly requires golang.org/x/sys@v0.47.0,
#   not golang.org/x/sys@v0.44.0
_REQUIRES = re.compile(r"requires (\S+)@(\S+), not \S+@\S+")


def arg_parse(arg_parser: ArgumentParser):
    arg_parser.formatter_class = argparse.RawTextHelpFormatter
    arg_parser.add_argument(
        "--branch",
        required=True,
        help="[required] Release branch to update, e.g. release-2.12.",
    )
    arg_parser.add_argument(
        "--repo",
        choices=list(config.get_dep_bump()["repos"]),
        default="",
        help="[optional] Only this repo (default: all configured repos).",
    )
    arg_parser.add_argument(
        "--skip-images",
        dest="skip_images",
        action="store_true",
        default=False,
        help="[optional] Don't refresh base images.",
    )
    arg_parser.add_argument(
        "--skip-go",
        dest="skip_go",
        action="store_true",
        default=False,
        help="[optional] Don't update Go dependencies.",
    )
    arg_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="[optional] Commit, push and open the PR. Without it, dry-run only.",
    )
    arg_parser.add_argument(
        "--report-dir",
        dest="report_dir",
        default="",
        help="[optional] Where the PR body is written (default from config).",
    )
    arg_parser.add_argument(
        "--update-existing",
        dest="update_existing",
        action="store_true",
        default=False,
        help="[optional] Replace the branch of an existing open dep_bump PR.\n"
        "This drops any commits added to it by hand (e.g. RPM lockfiles).",
    )


def _first_line(error: Exception) -> str:
    """The first informative line of a command error (skips `# pkg` headers
    and download chatter)."""
    lines = [
        ln.strip()
        for ln in str(error).splitlines()
        if ln.strip() and not ln.startswith(("# ", "go: downloading"))
    ]
    return (lines[0] if lines else str(error).strip())[:300]


def _find_existing_pr(gh_repo: str, branch: str, title: str) -> str:
    """Head branch of an open PR with our title against *branch*, else ''."""
    try:
        prs = GHCLI(".").list_open_prs_for_base(base=branch, repo=gh_repo)
    except RuntimeError as e:
        logger.warning(f"Could not list open PRs on {gh_repo}: {e}")
        return ""
    return next(
        (p.get("headRefName", "") for p in prs if p.get("title") == title), ""
    )


class _Run:
    """State for one repo + branch run."""

    def __init__(self, repo_dir: str, git: Git, repo_cfg: dict, cfg: dict):
        self.repo_dir = repo_dir
        self.git = git
        self.repo_cfg = repo_cfg
        self.cfg = cfg
        self.tags: dict[str, list[str]] = {}
        self.changed: dict[str, set[str]] = {}  # Containerfile -> stages
        self.timeout = config.get_timeouts().get("go_command_seconds", 1800)

    def tags_of(self, repo: str) -> list[str]:
        if repo not in self.tags:
            self.tags[repo] = Skopeo().list_tags(repo)
        return self.tags[repo]

    def read(self, rel: str) -> str:
        with open(os.path.join(self.repo_dir, rel)) as f:
            return f.read()

    def write(self, rel: str, text: str) -> None:
        with open(os.path.join(self.repo_dir, rel), "w") as f:
            f.write(text)

    def record_image(
        self, result, rel: str, stage: str, old: str, new: str, reason: str
    ) -> None:
        result.images.append(
            ImageBumpDTO(
                file=rel, stage=stage, old=old, new=new, reason=reason
            )
        )
        self.changed.setdefault(rel, set()).add(stage)


# --- base images ----------------------------------------------------------------


def _refresh_ref(run: _Run, ref: str) -> str | None:
    """Newest same-stream replacement for *ref*, or None if current/unknown."""
    repo, tag, digest = split_ref(ref)
    if tag:
        newest = newest_in_stream(tag, run.tags_of(repo))
        return f"{repo}:{newest}" if newest and newest != tag else None
    if not digest:
        return None
    labels = Skopeo().inspect(ref, arch="amd64").get("Labels", {})
    stream, el = ocp_stream(
        labels.get("version", ""), labels.get("release", "")
    )
    if not stream:
        return None
    newest = newest_ocp_tag(run.tags_of(repo), stream, el)
    if not newest:
        return None
    raw = Skopeo().inspect_raw(f"{repo}:{newest}")
    new_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return f"{repo}@{new_digest}" if new_digest != digest else None


def bump_images(run: _Run, result: DepBumpRepoResultDTO) -> None:
    registry = run.cfg["registry"] + "/"
    for rel in containerfiles(
        run.repo_dir, run.repo_cfg["containerfiles"], run.repo_cfg["exclude"]
    ):
        text = run.read(rel)
        new_text = text
        for line in parse_from_lines(text):
            if not line.ref.startswith(registry):
                continue
            try:
                new_ref = _refresh_ref(run, line.ref)
            except RuntimeError as e:
                _reject(
                    result,
                    rel,
                    line.ref,
                    f"tag lookup failed: {_first_line(e)}",
                )
                continue
            if new_ref:
                new_text = rewrite_ref(new_text, line.index, line.ref, new_ref)
                run.record_image(
                    result, rel, line.stage, line.ref, new_ref, "newest build"
                )
        if new_text != text:
            run.write(rel, new_text)


# --- Go modules -----------------------------------------------------------------


def _module_paths(rel: str, vendor: bool) -> list[str]:
    files = ["go.mod", "go.sum"] + (["vendor"] if vendor else [])
    return [os.path.normpath(os.path.join(rel, f)) for f in files]


class _Module:
    def __init__(self, run: _Run, rel: str, mod_cfg: dict):
        self.run = run
        self.rel = rel
        self.cfg = mod_cfg
        self.vendor = mod_cfg["vendor"]
        self.go = Go(
            os.path.join(run.repo_dir, rel), run.timeout, vendor=self.vendor
        )
        self.paths = _module_paths(rel, self.vendor)
        self.builders = mod_cfg.get("builders", [])
        # Set for modules replacing forklift => ../../ once root changed:
        # their clean state is HEAD plus a re-sync with the new root.
        self.resync = False
        # Why a tidy/vendor probe failed on the committed module, if it did;
        # such a module can't take bumps and is reported once.
        self.broken = ""

    def restore(self) -> None:
        """Back to the branch's committed state."""
        self.run.git.restore_paths(self.paths)

    def reset(self) -> None:
        """Back to this module's clean starting point for a bump attempt."""
        self.restore()
        if self.resync:
            self.sync()

    def mod(self) -> ModFile:
        return parse_mod_json(self.go.edit_json())

    def sync(self) -> None:
        # A tool-pinning module has no imports, so `go mod tidy` would drop
        # every requirement; it only needs go.sum filled for its build.
        if self.cfg.get("tidy", True):
            self.go.tidy()
        else:
            self.go.fill_sums(self.cfg["build"])
        if self.vendor:
            self.go.vendor_modules()

    def build(self) -> None:
        self.go.build(self.cfg["build"])

    def tidy_probe(self, result) -> bool:
        """Whether tidy/vendor on the clean module reproduces it and builds.

        A module that isn't tidy, or whose vendor/ is stale, would make every
        bump look like a cascade or a breakage; report it once instead.
        """
        if self.broken:
            return False
        original = self.mod()
        guard = self.run.cfg["cascade_guard"]
        try:
            self.sync()
            tidied = self.mod()
            drift = cascades(original, tidied, set(), guard)
            if not drift and tidied.go == original.go:
                self.build()
        except GoCommandError as e:
            self.reset()
            self.broken = (
                "re-running `go mod tidy`/`go mod vendor` with no bump "
                f"breaks the build ({_first_line(e)})"
            )
        else:
            self.reset()
            if drift or tidied.go != original.go:
                self.broken = (
                    "module is not tidy-clean (`go mod tidy` alone moves "
                    + "; ".join(
                        drift[:3] or [f"go {original.go} -> {tidied.go}"]
                    )
                    + ")"
                )
        if self.broken:
            _reject(
                result, self.rel, "*", f"{self.broken}; needs a manual update"
            )
        return not self.broken

    def scan(self) -> list[dict]:
        """govulncheck findings; a single-binary module scans its binary."""
        target = self.cfg["build"]
        if target.startswith("./"):
            return self.go.govulncheck()
        tmp = create_temp_dir("dep-bump-scan")
        try:
            binary = os.path.join(tmp.name, "bin")
            self.go.build(target, out=binary)
            return self.go.govulncheck(binary=binary)
        finally:
            tmp.cleanup()

    def allow_go_raise(self) -> bool:
        """Only when every builder is free of RPM lockfiles."""
        if not self.builders:
            return False
        stages = {
            b: {ln.stage for ln in parse_from_lines(self.run.read(b))}
            for b in self.builders
        }
        return not rpm_components(
            self.run.repo_dir, self.run.repo_cfg["rpm_inputs"], stages
        )

    def builder_bumps(self, directive: str) -> list[tuple] | str:
        """(file, line, stage, old, new) go-toolset edits for *directive*.

        Returns an error string when no go-toolset build supports it.
        """
        want = tuple(int(p) for p in directive.split("."))
        edits = []
        for rel in self.builders:
            for line in parse_from_lines(self.run.read(rel)):
                current = go_toolset_version(line.ref)
                if current is None or current >= want:
                    continue
                repo, _, _ = split_ref(line.ref)
                tag = go_toolset_for(directive, self.run.tags_of(repo))
                if not tag:
                    return f"no {repo} build supports go {directive}"
                edits.append(
                    (rel, line.index, line.stage, line.ref, f"{repo}:{tag}")
                )
        return edits

    def _get(self, cands: list[Candidate]) -> None:
        """`go get` the targets, raising any target another one outgrows.

        Exact pins conflict when one target needs a newer version of another
        (x/text v0.41.0 needs x/sys >= v0.47.0 while x/sys's own fix is
        v0.44.0); `go get` then names the version it needs, so take it.
        """
        by_path = {c.path: c for c in cands}
        for _ in range(len(cands) + 1):
            try:
                self.go.get([c.spec for c in cands])
                return
            except GoCommandError as e:
                need = _REQUIRES.search(str(e))
                cand = by_path.get(need.group(1)) if need else None
                if not cand or semver(need.group(2)) <= semver(cand.version):
                    raise
                logger.info(
                    f"{self.rel}: raising {cand.path} target "
                    f"{cand.version} -> {need.group(2)} (required by "
                    "another bump)"
                )
                self.reset()
                cand.version = need.group(2)
        self.go.get([c.spec for c in cands])

    def attempt(
        self, original: ModFile, cands: list[Candidate]
    ) -> tuple[bool, str, ModFile | None]:
        """Apply *cands* on a clean module; (ok, reason, resulting go.mod)."""
        guard = self.run.cfg["cascade_guard"]
        try:
            self.reset()
            self._get(cands)
            got = self.mod()
            if got.toolchain != original.toolchain:
                # `go get` adds a toolchain line when it raises the go
                # directive; keep the old one unless it's now below go.
                old = original.toolchain.removeprefix("go")
                keep = old and semver(old) >= semver(got.go)
                self.go.set_toolchain(original.toolchain if keep else "")
            self.sync()
            after = self.mod()
        except GoCommandError as e:
            return False, f"go get/tidy failed: {_first_line(e)}", None
        moved = cascades(original, after, {c.path for c in cands}, guard)
        if moved:
            return False, "cascades: " + "; ".join(moved[:5]), None
        if after.go != original.go:
            if not self.allow_go_raise():
                return (
                    False,
                    f"needs go {after.go} (module is on {original.go}) and "
                    "its builders have RPM lockfiles or aren't configured",
                    None,
                )
            edits = self.builder_bumps(after.go)
            if isinstance(edits, str):
                return False, edits, None
        try:
            self.build()
        except GoCommandError as e:
            return (
                False,
                f"build fails (likely needs code changes): {_first_line(e)}",
                None,
            )
        return True, "", after


def _record_go(
    module: _Module,
    result: DepBumpRepoResultDTO,
    original: ModFile,
    after: ModFile,
    cands: list[Candidate],
) -> None:
    rel = module.rel
    for c in cands:
        result.go_bumps.append(
            GoBumpDTO(
                module=rel,
                path=c.path,
                old=original.requires.get(c.path, ("", False))[0],
                new=after.requires.get(c.path, (c.version, False))[0],
                kind=c.kind,
                ids=c.ids,
            )
        )
    if after.go != original.go:
        result.go_bumps.append(
            GoBumpDTO(
                module=rel,
                path="go",
                old=original.go,
                new=after.go,
                kind="go-directive",
            )
        )
        for file, index, stage, old, new in module.builder_bumps(after.go):
            text = module.run.read(file)
            module.run.write(file, rewrite_ref(text, index, old, new))
            module.run.record_image(
                result,
                file,
                stage,
                old,
                new,
                f"go {after.go} needed by `{rel}`",
            )
    result.go_bumps.extend(
        side_bumps(rel, original, after, {c.path for c in cands})
    )


def _reject(result, module: str, path: str, reason: str, target: str = ""):
    logger.info(f"Rejected {module} {path} {target}: {reason}")
    result.rejected.append(
        RejectedBumpDTO(module=module, path=path, target=target, reason=reason)
    )


def _latest_patch(module: _Module, cand: Candidate) -> None:
    """Move a security target to the newest release of its fixed minor.

    The Go vulnerability database doesn't carry every CVE PSIRT files (e.g.
    CVE-2026-88031 for mongo-driver isn't in it, while GO-2026-5327 is), so
    taking the newest patch also picks up fixes it doesn't know about.
    """
    fixed = semver(cand.version)
    newer = [
        v
        for v in module.go.versions(cand.path)
        if "-" not in v.lstrip("v")
        and semver(v)[:2] == fixed[:2]
        and semver(v) > fixed
    ]
    if newer:
        cand.version = max(newer, key=semver)


class _Bumper:
    """Tries candidate sets on one module, keeping what passes."""

    def __init__(self, module: _Module, result, original: ModFile):
        self.module = module
        self.result = result
        self.original = original
        self.accepted: list[Candidate] = []
        self.after: ModFile | None = None
        self.attempts_left = module.run.cfg["max_single_attempts"]

    def _try(self, cands: list[Candidate]) -> tuple[bool, str]:
        # attempt() may raise targets to satisfy each other; work on copies
        # so a failed attempt doesn't leak inflated versions into the next.
        trial = [replace(c) for c in [*self.accepted, *cands]]
        ok, reason, after = self.module.attempt(self.original, trial)
        if ok:
            self.accepted = trial
            self.after = after
        return ok, reason

    def phase(self, cands: list[Candidate]) -> None:
        """All of *cands* at once, else one at a time."""
        if not cands:
            return
        ok, reason = self._try(cands)
        if ok:
            return
        rel = self.module.rel
        logger.info(f"{rel}: combined bump failed ({reason}); trying singly")
        for cand in cands:
            if self.attempts_left <= 0:
                _reject(
                    self.result,
                    rel,
                    cand.path,
                    "not attempted (max_single_attempts)",
                    cand.version,
                )
                continue
            self.attempts_left -= 1
            ok, reason = self._try([cand])
            if not ok:
                _reject(self.result, rel, cand.path, reason, cand.version)
        # Leave the module in the accepted state for the next phase.
        if self.accepted:
            ok, reason = self._try([])
            if not ok:  # shouldn't happen: the last accepted attempt passed
                _reject(
                    self.result, rel, "*", f"final bump set failed: {reason}"
                )
                self.accepted, self.after = [], None
        if not self.accepted:
            self.module.reset()


def update_module(module: _Module, result: DepBumpRepoResultDTO) -> bool:
    """Apply the safe bumps to one module; True if anything changed."""
    rel = module.rel
    if not module.tidy_probe(result):
        return False
    try:
        module.build()
    except GoCommandError as e:
        _reject(result, rel, "*", f"baseline build fails: {_first_line(e)}")
        return False
    original = module.mod()

    bumper = _Bumper(module, result, original)
    try:
        security, unfixable = security_candidates(module.scan())
        for cand in security.values():
            _latest_patch(module, cand)
    except GoCommandError as e:
        _reject(result, rel, "*", f"govulncheck failed: {_first_line(e)}")
        return False
    for entry in unfixable:
        _reject(result, rel, entry, "no fixed version available upstream")
    bumper.phase(list(security.values()))

    # Patch bumps are listed against the post-security state, so they don't
    # fight the versions the security bumps already pulled in.
    try:
        patches = patch_candidates(module.go.list_updates(), set(security))
    except GoCommandError as e:
        _reject(result, rel, "*", f"could not list updates: {_first_line(e)}")
        patches = []
    bumper.phase(patches)

    if not bumper.accepted:
        return False
    _record_go(module, result, original, bumper.after, bumper.accepted)
    return True


def bump_go(run: _Run, result: DepBumpRepoResultDTO) -> None:
    modules = []
    for mod_cfg in run.repo_cfg["go_modules"]:
        rel = next(
            (
                d
                for d in mod_cfg["dirs"]
                if os.path.exists(os.path.join(run.repo_dir, d, "go.mod"))
            ),
            None,
        )
        if rel is not None:
            modules.append(_Module(run, rel, mod_cfg))
    if not modules:
        return

    root, rest = modules[0], modules[1:]
    # Modules that replace forklift => ../../ vendor root packages, so root's
    # new requirements must flow into them and still build. Probe them while
    # everything is pristine: one that can't re-vendor even with no root
    # change is already broken and must not veto root's bumps — #8853 left
    # cmd/kubectl-mtv's committed vendor untouched for that reason.
    dependents = [
        m
        for m in rest
        if "github.com/kubev2v/forklift" in m.mod().replaced
        and m.tidy_probe(result)
    ]
    root_changed = update_module(root, result)
    root_bumps = len(result.go_bumps)

    if root_changed:
        synced = []
        for dep in dependents:
            before = dep.mod()
            try:
                dep.sync()
                dep.build()
            except GoCommandError as e:
                reason = f"breaks `{dep.rel}`: {_first_line(e)}"
                for m in [root, *dependents]:
                    m.restore()
                    m.resync = False
                for bump in result.go_bumps[:root_bumps]:
                    if bump.kind in ("security", "patch"):
                        _reject(result, root.rel, bump.path, reason, bump.new)
                del result.go_bumps[:root_bumps]
                synced = []
                break
            dep.resync = True
            synced.append(side_bumps(dep.rel, before, dep.mod(), set()))
        for bumps in synced:
            result.go_bumps.extend(bumps)

    for module in rest:
        update_module(module, result)


# --- PR -------------------------------------------------------------------------


def _write_report(report_dir: str, name: str, branch: str, body: str) -> str:
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"{name}-{branch}.md")
    with open(path, "w") as f:
        f.write(body)
    return path


def _push(
    run: _Run,
    gh_repo: str,
    branch: str,
    title: str,
    body: str,
    commit: str,
    existing: str,
) -> None:
    git = run.git
    git.config("user.email", config.get_git_email())
    git.config("user.name", config.get_git_name())
    GHCLI(run.repo_dir).auth()
    version = branch.removeprefix("release-")
    if existing:
        head = existing
        # Make the remote-tracking ref exist so --force-with-lease can check
        # that nobody pushed since we looked.
        git.repo.git.fetch(
            "origin", f"+refs/heads/{head}:refs/remotes/origin/{head}"
        )
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        head = f"{run.cfg['branch_prefix']}-{version}-{stamp}"
    git.checkout(head, create=True)
    git.add_paths(["-A"])
    git.commit(commit)
    git.push(branch=head, force="lease" if existing else None)
    if existing:
        GHCLI(run.repo_dir).edit_pr_body(gh_repo, head, body)
    else:
        GHCLI(run.repo_dir).create_pr_for_repo(
            repo=gh_repo, title=title, body=body, base=branch, head=head
        )


async def process_repo(
    name: str, cfg: dict, args: Namespace
) -> DepBumpRepoResultDTO:
    repo_cfg = cfg["repos"][name]
    branch = args.branch
    result = DepBumpRepoResultDTO(
        repo=name, branch=branch, dry_run=not args.apply
    )
    title = cfg["pr_title"].format(version=branch.removeprefix("release-"))

    existing = _find_existing_pr(repo_cfg["gh_repo"], branch, title)
    if existing and not args.update_existing:
        result.skipped = True
        result.skip_reason = (
            f"open PR from {existing} already exists; merge or close it, or "
            "pass --update-existing (replaces its branch)"
        )
        return result

    tmp = create_temp_dir(f"dep-bump-{name}")
    try:
        git = Git(tmp.name)
        try:
            await git.clone(repo_cfg["url"], depth=1, branch=branch)
        except gitpython.GitCommandError as e:
            result.skipped = True
            result.skip_reason = f"clone of {branch} failed: {_first_line(e)}"
            return result
        run = _Run(tmp.name, git, repo_cfg, cfg)
        if not args.skip_images:
            bump_images(run, result)
        if not args.skip_go:
            bump_go(run, result)
        result.rpm_components = rpm_components(
            tmp.name, repo_cfg["rpm_inputs"], run.changed
        )

        changed = bool(git.changed_paths())
        if changed or result.rejected:
            body = render_body(result, cfg["rpm_script"])
            report = _write_report(
                args.report_dir or cfg["report_dir"], name, branch, body
            )
            logger.info(f"{name}@{branch}: report written to {report}")
        if not changed:
            logger.info(f"{name}@{branch}: nothing to update")
            return result
        if not args.apply:
            logger.info(
                f"DRY RUN: would open '{title}' on {repo_cfg['gh_repo']}"
            )
            return result
        _push(
            run,
            repo_cfg["gh_repo"],
            branch,
            title,
            body,
            render_commit(result, title),
            existing,
        )
        return result
    finally:
        tmp.cleanup()


@task
async def dep_bump(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> DepBumpResultDTO:
    cfg = config.get_dep_bump()
    names = [args.repo] if args.repo else list(cfg["repos"])
    logger.info(
        {
            "msg": "Starting dep_bump",
            "branch": args.branch,
            "repos": names,
            "dry_run": not args.apply,
        }
    )
    try:
        Skopeo().auth_prod()
    except RuntimeError as e:
        # Tag lookups that then fail are reported per image.
        logger.warning(f"registry login failed: {_first_line(e)}")

    results = []
    for name in names:
        res = await process_repo(name, cfg, args)
        logger.info(
            {
                "msg": "dep_bump summary",
                "repo": name,
                "images": len(res.images),
                "go_bumps": len(res.go_bumps),
                "rejected": len(res.rejected),
                "rpm_components": res.rpm_components,
                "skipped": res.skip_reason,
            }
        )
        results.append(res)
    return DepBumpResultDTO(repos=results)
