"""Helpers for the dep_bump pipeline (plain functions, no @task).

Base images are refreshed to the newest build of the *same* version stream
(go-toolset 1.25.x, ubi9-minimal 9.7, ose-* v4.18). Go modules get security
fixes and patch bumps, kept only if they don't cascade into guarded
ecosystems, don't need a go directive raise the module isn't allowed, and
still build.
"""

import glob
import os
import re
from dataclasses import dataclass, field

import yaml
from models.dto import DepBumpRepoResultDTO, GoBumpDTO

_FROM = re.compile(r"^(FROM\s+(?:--\S+\s+)*)(\S+)(.*)$", re.IGNORECASE)
_STAGE = re.compile(r"\bAS\s+(\S+)", re.IGNORECASE)
_VERSION_TAG = re.compile(r"^(\d+(?:\.\d+)*)-(\d+)$")
_OCP_TAG = re.compile(
    r"^v(\d+\.\d+)\.\d+-(\d{12})\.p(\d+)\.g[0-9a-f]+\.assembly\.stream\.(el\d+)$"
)
_PSEUDO = re.compile(r"\d{14}-[0-9a-f]{12}$")
_GO_TOOLSET = re.compile(r"/go-toolset$")


# --- image refs and tags --------------------------------------------------------


@dataclass
class FromLine:
    index: int  # line number in the file
    ref: str
    stage: str


def parse_from_lines(text: str) -> list[FromLine]:
    lines = []
    for i, line in enumerate(text.splitlines()):
        m = _FROM.match(line.strip())
        if m:
            stage = _STAGE.search(m.group(3))
            lines.append(
                FromLine(i, m.group(2), stage.group(1) if stage else "")
            )
    return lines


def split_ref(ref: str) -> tuple[str, str, str]:
    """registry/ns/name:tag or @sha256:x -> (repo, tag, digest)."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, "", digest
    name = ref.rsplit("/", 1)[-1]
    if ":" in name:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag, ""
    return ref, "", ""


def _version(text: str) -> tuple:
    return tuple(int(p) for p in text.split("."))


def newest_in_stream(current: str, tags: list[str]) -> str | None:
    """Newest tag in *current*'s stream; None if *current* isn't recognized.

    Versioned tags keep major.minor (a single-part version keeps the major);
    OpenShift tags keep vX.Y and the elN suffix. -source tags never match.
    """
    m = _VERSION_TAG.match(current)
    if m:
        version = _version(m.group(1))
        width = min(len(version), 2)
        best = (version, int(m.group(2)), current)
        for tag in tags:
            t = _VERSION_TAG.match(tag)
            if not t:
                continue
            tv = _version(t.group(1))
            if len(tv) != len(version) or tv[:width] != version[:width]:
                continue
            best = max(best, (tv, int(t.group(2)), tag))
        return best[2]

    m = _OCP_TAG.match(current)
    if m:
        stream, el = m.group(1), m.group(4)
        best = (m.group(2), int(m.group(3)), current)
        for tag in tags:
            t = _OCP_TAG.match(tag)
            if t and t.group(1) == stream and t.group(4) == el:
                best = max(best, (t.group(2), int(t.group(3)), tag))
        return best[2]
    return None


def ocp_stream(version_label: str, release_label: str) -> tuple[str, str]:
    """Labels of an ose-* image -> (stream, elN), e.g. ('4.16', 'el9')."""
    stream = re.match(r"v?(\d+\.\d+)", version_label or "")
    el = re.search(r"\.(el\d+)$", release_label or "")
    return (stream.group(1) if stream else "", el.group(1) if el else "")


def newest_ocp_tag(tags: list[str], stream: str, el: str) -> str | None:
    best = None
    for tag in tags:
        t = _OCP_TAG.match(tag)
        if t and t.group(1) == stream and t.group(4) == el:
            key = (t.group(2), int(t.group(3)), tag)
            best = max(best, key) if best else key
    return best[2] if best else None


def go_toolset_for(go_directive: str, tags: list[str]) -> str | None:
    """Newest go-toolset tag whose Go version is >= *go_directive*.

    Stays on the directive's major.minor so a raise to go 1.25.0 picks a
    1.25.x builder rather than jumping to the newest Go.
    """
    want = _version(go_directive)
    best = None
    for tag in tags:
        t = _VERSION_TAG.match(tag)
        if not t:
            continue
        tv = _version(t.group(1))
        if len(tv) < 2 or tv[:2] != want[:2] or tv < want:
            continue
        key = (tv, int(t.group(2)), tag)
        best = max(best, key) if best else key
    return best[2] if best else None


def go_toolset_version(ref: str) -> tuple | None:
    repo, tag, _ = split_ref(ref)
    m = _VERSION_TAG.match(tag) if _GO_TOOLSET.search(repo) else None
    return _version(m.group(1)) if m else None


def rewrite_ref(text: str, line_index: int, old: str, new: str) -> str:
    lines = text.split("\n")
    lines[line_index] = lines[line_index].replace(old, new, 1)
    return "\n".join(lines)


def containerfiles(repo_dir: str, patterns: list[str], exclude: list[str]):
    found = set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(repo_dir, pattern), recursive=True):
            rel = os.path.relpath(path, repo_dir)
            if os.path.isfile(path) and not rel.startswith(tuple(exclude)):
                found.add(rel)
    return sorted(found)


def rpm_components(
    repo_dir: str, pattern: str, changed: dict[str, set[str]]
) -> list[str]:
    """rpms.in.yaml dirs whose Containerfile stage had its FROM changed.

    *changed* maps a Containerfile path to the stage names whose FROM moved
    ("" for an unnamed stage). Without a stageName any change counts.
    """
    components = []
    for path in sorted(
        glob.glob(os.path.join(repo_dir, pattern), recursive=True)
    ):
        with open(path) as f:
            spec = yaml.safe_load(f) or {}
        cf = (spec.get("context") or {}).get("containerfile") or {}
        if not cf.get("file"):
            continue
        base = os.path.dirname(path)
        target = os.path.relpath(
            os.path.normpath(os.path.join(base, cf["file"])), repo_dir
        )
        stages = changed.get(target)
        if stages is None:
            continue
        stage = cf.get("stageName")
        if stage is None or stage in stages:
            components.append(os.path.relpath(base, repo_dir))
    return components


# --- Go modules ---------------------------------------------------------------


@dataclass
class Candidate:
    path: str
    version: str
    kind: str  # security | patch
    ids: list[str] = field(default_factory=list)

    @property
    def spec(self) -> str:
        return f"{self.path}@{self.version}"


@dataclass
class ModFile:
    go: str
    toolchain: str
    requires: dict[str, tuple[str, bool]]  # path -> (version, indirect)
    replaced: set[str]


def parse_mod_json(data: dict) -> ModFile:
    return ModFile(
        go=data.get("Go", ""),
        toolchain=data.get("Toolchain", ""),
        requires={
            r["Path"]: (r["Version"], bool(r.get("Indirect")))
            for r in data.get("Require") or []
        },
        replaced={r["Old"]["Path"] for r in data.get("Replace") or []},
    )


def semver(version: str) -> tuple:
    core = version.lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for piece in core.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) + (0,) * (3 - len(parts))


def is_pseudo(version: str) -> bool:
    return bool(_PSEUDO.search(version))


def security_candidates(messages: list[dict]) -> tuple[dict, list[str]]:
    """govulncheck -scan module output -> ({module: Candidate}, unfixable).

    A module's target is the highest fixed version among its findings, the
    smallest version that fixes all of them.
    """
    aliases = {}
    for msg in messages:
        osv = msg.get("osv")
        if osv:
            aliases[osv["id"]] = [osv["id"], *osv.get("aliases", [])]

    candidates: dict[str, Candidate] = {}
    unfixable = []
    for msg in messages:
        finding = msg.get("finding")
        if not finding or not finding.get("trace"):
            continue
        module = finding["trace"][0].get("module", "")
        if module in ("stdlib", "toolchain", ""):
            continue
        ids = aliases.get(finding["osv"], [finding["osv"]])
        fixed = finding.get("fixed_version")
        if not fixed:
            unfixable.append(f"{module}: {', '.join(ids)}")
            continue
        cand = candidates.setdefault(
            module, Candidate(module, fixed, "security")
        )
        if semver(fixed) > semver(cand.version):
            cand.version = fixed
        cand.ids.extend(i for i in ids if i not in cand.ids)
    return candidates, sorted(set(unfixable))


def patch_candidates(modules: list[dict], skip: set[str]) -> list[Candidate]:
    """Same-major.minor updates of direct, unreplaced, non-pseudo deps."""
    result = []
    for m in modules:
        update = (m.get("Update") or {}).get("Version")
        current = m.get("Version", "")
        if (
            m.get("Main")
            or m.get("Indirect")
            or m.get("Replace")
            or not update
            or m["Path"] in skip
            or is_pseudo(current)
            or is_pseudo(update)
        ):
            continue
        if semver(update)[:2] == semver(current)[:2] and semver(
            update
        ) > semver(current):
            result.append(Candidate(m["Path"], update, "patch"))
    return result


def cascades(
    before: ModFile, after: ModFile, targets: set[str], guard: list[str]
) -> list[str]:
    """Requirements that moved too far as a side effect of the bumps.

    Any major move counts; a minor move counts only in guarded ecosystems
    (k8s, kubevirt, ...). Sibling drags like golang.org/x/* minors are fine.
    """
    moved = []
    for path, (old, _) in before.requires.items():
        if path in targets or path not in after.requires:
            continue
        new = after.requires[path][0]
        o, n = semver(old), semver(new)
        if n[0] != o[0] or (n[:2] != o[:2] and path.startswith(tuple(guard))):
            moved.append(f"{path} {old} -> {new}")
    return moved


def side_bumps(
    module: str, before: ModFile, after: ModFile, targets: set[str]
) -> list[GoBumpDTO]:
    """Requirements that changed alongside the bump targets."""
    bumps = []
    for path, (new, _) in sorted(after.requires.items()):
        old = before.requires.get(path, ("", False))[0]
        if path in targets or old == new:
            continue
        bumps.append(
            GoBumpDTO(
                module=module, path=path, old=old, new=new, kind="dependency"
            )
        )
    return bumps


# --- rendering ------------------------------------------------------------------


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def render_body(result: DepBumpRepoResultDTO, rpm_script: str) -> str:
    out = [
        f"Automated dependency and base image refresh for `{result.branch}`, "
        "generated by the mtv-releng `dep_bump` pipeline.",
        "",
        "RPM lockfiles, `.tekton` digests, bundle image nudges and "
        "`release.conf` are intentionally not touched.",
    ]
    if result.images:
        out += [
            "",
            "## Base images",
            "",
            "| Containerfile | Stage | Old | New | Why |",
            "|---|---|---|---|---|",
        ]
        out += [
            f"| `{i.file}` | {i.stage} | `{_md(i.old)}` | `{_md(i.new)}` "
            f"| {i.reason} |"
            for i in result.images
        ]
    if result.go_bumps:
        out += [
            "",
            "## Go modules",
            "",
            "| Module | Dependency | Old | New | Kind | Advisories |",
            "|---|---|---|---|---|---|",
        ]
        out += [
            f"| `{b.module}` | `{b.path}` | {b.old} | {b.new} | {b.kind} "
            f"| {', '.join(b.ids)} |"
            for b in result.go_bumps
        ]
    if result.rejected:
        out += ["", "## Not included", ""]
        out += [
            f"- `{r.module}` `{r.path}`"
            + (f" -> {r.target}" if r.target else "")
            + f": {r.reason}"
            for r in result.rejected
        ]
    if result.rpm_components:
        comps = " ".join(result.rpm_components)
        out += [
            "",
            "## RPM lockfiles need a refresh",
            "",
            "These components' base images changed and their "
            "`rpms.lock.yaml` is resolved against the base image. On an "
            "x86_64 VM, from a checkout of this PR branch:",
            "",
            "```bash",
            f"REGISTRY_AUTH_USER=... REGISTRY_AUTH_PASS=... \\\n"
            f"  {rpm_script} /path/to/checkout {comps}",
            "```",
            "",
            "Then commit the regenerated `redhat.repo` / `rpms.lock.yaml` "
            "files to this branch.",
        ]
    return "\n".join(out) + "\n"


def render_commit(result: DepBumpRepoResultDTO, title: str) -> str:
    lines = [title, ""]
    if result.images:
        lines.append(f"- Refresh {len(result.images)} base image reference(s)")
    by_module: dict[str, list[GoBumpDTO]] = {}
    for b in result.go_bumps:
        if b.kind in ("security", "patch", "go-directive"):
            by_module.setdefault(b.module, []).append(b)
    for module, bumps in by_module.items():
        lines.append(
            f"- {module}: "
            + ", ".join(f"{b.path} {b.old} -> {b.new}" for b in bumps)
        )
    if result.rpm_components:
        lines.append("- RPM lockfiles still need a refresh (see PR)")
    return "\n".join(lines)
