"""Helpers for the dep_bump pipeline (plain functions, no @task).

Base images are refreshed to the newest build of the same major (go-toolset
1.x, ubi9-minimal 9.x), so out-of-support minors move forward; ose-* images
stay on their OpenShift stream while it is inside the release's supported
OCP range (release.conf OCP_VERSIONS) and move into it otherwise. Go
modules get security
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
_OCP_RANGE = re.compile(
    r"^OCP_VERSIONS=v?(\d+\.\d+)-v?(\d+\.\d+)\s*$", re.MULTILINE
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


def newest_in_stream(
    current: str, tags: list[str], settled_before: int = 0
) -> str | None:
    """Newest tag in *current*'s stream; None if *current* isn't recognized.

    Versioned tags keep the major and the number of version parts, so minors
    jump (ubi 9.6 -> 9.8, go-toolset 1.25.9 -> 1.26.10) but a 1.25.9 never
    lands on a floating 1.26 or a RHEL-versioned 9.8 go-toolset tag. Another
    minor is only taken once it has settled: its first build (the epoch
    suffix of the tag) is at or before *settled_before*.
    OpenShift tags keep vX.Y and the elN suffix. -source tags never match.
    """
    m = _VERSION_TAG.match(current)
    if m:
        version = _version(m.group(1))
        stream = []
        first_build: dict[tuple, int] = {}
        for tag in tags:
            t = _VERSION_TAG.match(tag)
            if not t:
                continue
            tv = _version(t.group(1))
            if len(tv) != len(version) or tv[0] != version[0]:
                continue
            build = int(t.group(2))
            stream.append((tv, build, tag))
            first_build[tv[:2]] = min(build, first_build.get(tv[:2], build))
        best = (version, int(m.group(2)), current)
        for tv, build, tag in stream:
            if tv[:2] == version[:2] or first_build[tv[:2]] <= settled_before:
                best = max(best, (tv, build, tag))
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


def ocp_tag_stream(tag: str) -> tuple[str, str]:
    """An ose-* tag -> (stream, elN), or ('', '') for any other tag."""
    m = _OCP_TAG.match(tag)
    return (m.group(1), m.group(4)) if m else ("", "")


def ocp_range(release_conf: str) -> tuple[str, str] | None:
    """OCP_VERSIONS=v4.20-v4.22 in a release.conf -> ('4.20', '4.22')."""
    m = _OCP_RANGE.search(release_conf or "")
    return (m.group(1), m.group(2)) if m else None


def ocp_target_stream(
    current: str, el: str, tags: list[str], supported: tuple[str, str] | None
) -> str:
    """The OpenShift stream an ose-* image should be on.

    Its own while inside the release's supported range; otherwise the lowest
    supported stream the image is published for (the one every supported
    cluster can run). When nothing in the range is published yet (main
    supports OpenShift releases still in development), the newest stream
    below it. Never moves down, and never above the range.
    """
    if not supported:
        return current
    low, high = _version(supported[0]), _version(supported[1])
    cur = _version(current)
    if cur >= low:
        return current
    published = sorted(
        {
            _version(t.group(1))
            for t in map(_OCP_TAG.match, tags)
            if t and t.group(4) == el and _version(t.group(1)) > cur
        }
    )
    in_range = [v for v in published if low <= v <= high]
    below = [v for v in published if v < low]
    best = in_range[0] if in_range else (below[-1] if below else cur)
    return ".".join(map(str, best))


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


# --- Jira ticket matching --------------------------------------------------------


# Both queries hit MTV "Vulnerability" tickets (the SecurityTracking tickets
# cve_triage triages); only open ones are worth resolving.
_TICKET_FIELDS = ["summary", "labels"]
# cve_triage labels BASE_BUMP_NEEDED and AFFECTED tickets "triage:needs-fix".
_BASE_BUMP_LABEL = "triage:needs-fix"
_TRIAGE_MARKER = "[cve-triage:BASE_BUMP_NEEDED]"
_BASE_REF = re.compile(r"Base image: (\S+)")


def cve_search_jql(cves: list[str]) -> str:
    quoted = ", ".join(f'"{c}"' for c in sorted(cves))
    return (
        "project = MTV AND issuetype = Vulnerability "
        "AND labels = SecurityTracking AND statusCategory != Done "
        f"AND labels IN ({quoted})"
    )


def base_search_jql() -> str:
    return (
        "project = MTV AND issuetype = Vulnerability "
        "AND labels = SecurityTracking AND statusCategory != Done "
        f'AND labels = "{_BASE_BUMP_LABEL}"'
    )


def tickets_on_branch(issues: list[dict], branch: str) -> list[dict]:
    """Tickets whose summary carries this branch's stream ("[mtv-2.12]")."""
    version = branch.removeprefix("release-")
    pattern = re.compile(rf"\[mtv-{re.escape(version)}\]")
    return [
        i
        for i in issues
        if pattern.search(i.get("fields", {}).get("summary", ""))
    ]


def ticket_cves(issues: list[dict], branch: str) -> dict[str, list[str]]:
    """{CVE label: [ticket keys]} for the tickets on *branch*'s stream."""
    matched: dict[str, list[str]] = {}
    for issue in tickets_on_branch(issues, branch):
        for label in issue.get("fields", {}).get("labels", []):
            if label.startswith("CVE-"):
                matched.setdefault(label, []).append(issue["key"])
    return matched


def base_ref_from_comments(comments: list[str]) -> str:
    """Base image ref from a ticket's cve-triage BASE_BUMP_NEEDED comment."""
    for body in comments:
        if _TRIAGE_MARKER in body:
            m = _BASE_REF.search(body)
            if m:
                return m.group(1)
    return ""


def link_jira(result: DepBumpRepoResultDTO, jira) -> None:
    """Attach MTV Vulnerability ticket keys to the bumps that resolve them.

    Two read-only matches:
    - security bumps: their govulncheck CVE ids against the tickets' CVE
      labels;
    - base image bumps: tickets cve_triage classified BASE_BUMP_NEEDED (its
      comment names the base image ref) against the refs being bumped, by
      repository.

    Only public advisory ids and ticket keys are recorded: the PR body is
    public while the trackers are not.
    """
    cves = sorted(
        {
            i
            for b in result.go_bumps
            if b.kind == "security"
            for i in b.ids
            if i.startswith("CVE-")
        }
    )
    if cves:
        by_cve = ticket_cves(
            jira.search(cve_search_jql(cves), _TICKET_FIELDS), result.branch
        )
        for cve, keys in by_cve.items():
            for b in result.go_bumps:
                if b.kind == "security" and cve in b.ids:
                    b.jira_keys.extend(
                        k for k in keys if k not in b.jira_keys
                    )
    if result.images:
        for issue in tickets_on_branch(
            jira.search(base_search_jql(), _TICKET_FIELDS), result.branch
        ):
            ref = base_ref_from_comments(jira.get_comments(issue["key"]))
            if not ref:
                continue
            repo = split_ref(ref)[0]
            for i in result.images:
                if split_ref(i.old)[0] == repo and issue["key"] not in i.jira_keys:
                    i.jira_keys.append(issue["key"])


# --- rendering ------------------------------------------------------------------


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def _jira_tickets(
    result: DepBumpRepoResultDTO,
) -> list[tuple[str, str]]:
    """[(key, what resolves it)] in bump order, each ticket once."""
    tickets: list[tuple[str, str]] = []
    seen = set()
    for b in result.go_bumps:
        if b.kind != "security":
            continue
        for key in b.jira_keys:
            if key not in seen:
                seen.add(key)
                tickets.append(
                    (key, f"`{b.path}` {b.old} -> {b.new} ({b.kind})")
                )
    for i in result.images:
        for key in i.jira_keys:
            if key not in seen:
                seen.add(key)
                tickets.append((key, f"base image `{_md(i.old)}` ({i.file})"))
    return tickets


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
            "(Both unset: the script falls back to `REGISTRY_PROD_USER`/"
            "`REGISTRY_PROD_TOKEN`, e.g. `set -a; source mtv-releng/.env; "
            "set +a`.)",
            "",
            "Then commit the regenerated `redhat.repo` / `rpms.lock.yaml` "
            "files to this branch.",
        ]
    tickets = _jira_tickets(result)
    if tickets:
        out += [
            "",
            "## Jira tickets",
            "",
            "| Ticket | Resolved by |",
            "|---|---|",
        ]
        out += [f"| {key} | {by} |" for key, by in tickets]
        out += ["", *(f"Resolves: {key}" for key, _ in tickets)]
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
    lines.extend(f"Resolves: {key}" for key, _ in _jira_tickets(result))
    return "\n".join(lines)
