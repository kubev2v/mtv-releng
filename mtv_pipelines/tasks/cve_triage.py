"""Helpers for the cve_triage pipeline (plain functions, no @task).

The triage decision is driven by what actually ships: the image digest pinned
in the branch's operator bundle, the Go buildinfo / Python dist-info inside it,
and, for libraries inherited from an OpenShift base image, OpenShift's own
OCPBUGS tracker for the same CVE and stream.
"""

import json
import os
import re
import tarfile
from dataclasses import dataclass, field

from models.dto import CveTicketDTO

FIXED_IN_BUILD = "FIXED_IN_BUILD"
NOT_AFFECTED_VERSION = "NOT_AFFECTED_VERSION"
NOT_AFFECTED_CODE_ABSENT = "NOT_AFFECTED_CODE_ABSENT"
NOT_AFFECTED_OCP = "NOT_AFFECTED_OCP"
NOT_BUILT = "NOT_BUILT"
BLOCKED_ON_OCP = "BLOCKED_ON_OCP"
BASE_BUMP_NEEDED = "BASE_BUMP_NEEDED"
AFFECTED = "AFFECTED"
NEEDS_REVIEW = "NEEDS_REVIEW"
SKIPPED = "SKIPPED"

# Categories that get a comment + label; the rest are report-only.
ACTIONABLE = (
    FIXED_IN_BUILD,
    NOT_AFFECTED_VERSION,
    NOT_AFFECTED_CODE_ABSENT,
    NOT_AFFECTED_OCP,
    NOT_BUILT,
    BLOCKED_ON_OCP,
    BASE_BUMP_NEEDED,
    AFFECTED,
)

COMMENT_MARKER = "[cve-triage:{category}]"
OCP_FIXED_RESOLUTIONS = ("Done", "Done-Errata", "Errata", "Fixed")

_GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"
_GO_DEP_LINE = re.compile(rb"(dep|=>)\t([^\t\n]+)\t([^\t\n]+)")
_DIST_INFO = re.compile(r"site-packages/([^/]+)\.dist-info/METADATA$")
_BRANCH = re.compile(r"\[mtv-(\d+\.\d+)\]")
_PSCOMPONENT = "pscomponent:migration-toolkit-virtualization/"
_VER = r"v?\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?"
_FIXED_CLAUSE = re.compile(
    rf"(?:prior to|before|until|fixed in|patched in)\s+(?:versions?\s+)?"
    rf"((?:{_VER})(?:(?:,\s*|\s+and\s+|\s+or\s+)(?:{_VER}))*)",
    re.IGNORECASE,
)
_RANGE_CLAUSE = re.compile(
    rf"(?:from|versions?)\s+({_VER})\s+(until|through|to)\s+({_VER})",
    re.IGNORECASE,
)


@dataclass
class VulnRange:
    introduced: tuple = (0,)
    fixed: tuple | None = None
    last_affected: tuple | None = None


@dataclass
class FoundPackage:
    name: str
    version: str
    location: str
    from_base: bool = False
    # Whether any known vulnerable package path is linked into the binary;
    # None when we have no paths to check against.
    paths_present: bool | None = None


@dataclass
class BaseImage:
    ref: str
    component: str  # e.g. openshift4/ose-cli-rhel9
    stream: str = ""  # e.g. 4.20
    layer_digests: set[str] = field(default_factory=set)


@dataclass
class OcpTracker:
    key: str
    status: str
    resolution: str


@dataclass
class Facts:
    ticket: CveTicketDTO
    image: str = ""
    not_built_reason: str = ""
    error: str = ""
    candidates: list[str] = field(default_factory=list)
    found: list[FoundPackage] = field(default_factory=list)
    ranges: list[VulnRange] = field(default_factory=list)
    base: BaseImage | None = None
    ocp: OcpTracker | None = None
    # Streams of the OCPBUGS trackers the search returned; None when the
    # search wasn't run or failed. Explains a miss in the evidence.
    ocp_streams: list[str] | None = None


# --- Jira ticket parsing ----------------------------------------------------


def adf_to_text(node) -> str:
    """Flatten an Atlassian Document Format node to plain text."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    text = "".join(adf_to_text(c) for c in node.get("content", []))
    if node.get("type") in ("paragraph", "heading", "listItem"):
        text += "\n"
    return text


def parse_ticket(issue: dict) -> CveTicketDTO:
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")
    labels = fields.get("labels", [])
    m = _BRANCH.search(summary)
    description = fields.get("description") or ""
    if isinstance(description, dict):
        description = adf_to_text(description)
    return CveTicketDTO(
        key=issue["key"],
        summary=summary,
        branch=f"release-{m.group(1)}" if m else "",
        cve=next((lbl for lbl in labels if lbl.startswith("CVE-")), ""),
        component=next(
            (
                lbl[len(_PSCOMPONENT) :]
                for lbl in labels
                if lbl.startswith(_PSCOMPONENT)
            ),
            "",
        ),
        due_date=fields.get("duedate") or "",
        status=(fields.get("status") or {}).get("name", ""),
        labels=labels,
        description=description,
        embargoed=summary.upper().startswith("EMBARGOED"),
    )


def flaw_title(summary: str) -> str:
    """'CVE-1 ns/cmp: gRPC-Go: DoS ... [mtv-2.10]' -> 'grpc-go: dos ...'."""
    title = summary.split(": ", 1)[1] if ": " in summary else summary
    return _BRANCH.sub("", title).strip().lower()


def candidates_from_title(summary: str, aliases: dict) -> list[str]:
    title = flaw_title(summary)
    names: list[str] = []
    for keyword, packages in aliases.items():
        if keyword in title:
            names.extend(p for p in packages if p not in names)
    return names


# --- Versions and ranges ----------------------------------------------------


def parse_version(version: str) -> tuple:
    """'v1.82.2' -> (1, 82, 2); '27.1.1+incompatible' -> (27, 1, 1).

    Pre-release / pseudo-version suffixes are ignored.
    """
    core = re.split(r"[+-]", version.strip().lstrip("v"), maxsplit=1)[0]
    parts = []
    for piece in core.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or (0,)


def _cmp(a: tuple, b: tuple) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def is_vulnerable(version: str, ranges: list[VulnRange]) -> bool:
    v = parse_version(version)
    for r in ranges:
        if _cmp(v, r.introduced) < 0:
            continue
        if r.fixed is not None and _cmp(v, r.fixed) < 0:
            return True
        if r.last_affected is not None and _cmp(v, r.last_affected) <= 0:
            return True
        if r.fixed is None and r.last_affected is None:
            return True
    return False


def predates_ranges(version: str, ranges: list[VulnRange]) -> bool:
    """True when *version* is older than every range's introduced version."""
    v = parse_version(version)
    return bool(ranges) and all(
        r.introduced != (0,) and _cmp(v, r.introduced) < 0 for r in ranges
    )


def ranges_from_text(text: str) -> list[VulnRange]:
    """Best-effort fixed versions from flaw prose ("Prior to 1.82.2 and ...")."""
    explicit = [
        VulnRange(
            introduced=parse_version(m.group(1)),
            fixed=parse_version(m.group(3))
            if m.group(2).lower() != "through"
            else None,
            last_affected=parse_version(m.group(3))
            if m.group(2).lower() == "through"
            else None,
        )
        for m in _RANGE_CLAUSE.finditer(text)
    ]
    if explicit:
        return explicit

    fixes = sorted(
        {
            parse_version(v)
            for m in _FIXED_CLAUSE.finditer(text)
            for v in re.findall(_VER, m.group(1))
        }
    )
    ranges = []
    for i, fix in enumerate(fixes):
        # Several fixes are parallel release lines: "prior to 1.82.2 and
        # 1.83.2" means each line is vulnerable up to its own fix.
        introduced = (0,) if i == 0 else fix[:2] + (0,)
        ranges.append(VulnRange(introduced=introduced, fixed=fix))
    return ranges


def normalize_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def osv_packages(records: list[dict]) -> tuple[dict, dict]:
    """Merge OSV records -> ({name: [VulnRange]}, {name: [import paths]})."""
    ranges: dict[str, list[VulnRange]] = {}
    imports: dict[str, list[str]] = {}
    for rec in records:
        for aff in rec.get("affected", []):
            name = normalize_name(aff.get("package", {}).get("name", ""))
            if not name or name == "stdlib":
                continue
            for r in aff.get("ranges", []):
                if r.get("type") not in ("SEMVER", "ECOSYSTEM"):
                    continue
                current = VulnRange()
                for ev in r.get("events", []):
                    if "introduced" in ev:
                        current = VulnRange(
                            introduced=parse_version(ev["introduced"])
                        )
                    elif "fixed" in ev:
                        current.fixed = parse_version(ev["fixed"])
                        ranges.setdefault(name, []).append(current)
                        current = VulnRange()
                    elif "last_affected" in ev:
                        current.last_affected = parse_version(
                            ev["last_affected"]
                        )
                        ranges.setdefault(name, []).append(current)
                        current = VulnRange()
            paths = [
                i["path"]
                for i in aff.get("ecosystem_specific", {}).get("imports", [])
                if i.get("path")
            ]
            if paths:
                imports.setdefault(name, []).extend(paths)

    # CVE and GHSA records often repeat the same ranges, and pre-release fix
    # versions collapse to empty ranges once the suffix is dropped.
    for name, found in ranges.items():
        unique = []
        for r in found:
            empty = r.fixed is not None and _cmp(r.introduced, r.fixed) >= 0
            if not empty and r not in unique:
                unique.append(r)
        ranges[name] = unique
    imports = {n: sorted(set(p)) for n, p in imports.items()}
    return ranges, imports


def osv_related_ids(record: dict) -> list[str]:
    ids = record.get("aliases", []) + record.get("related", [])
    return [i for i in ids if i.split("-")[0] in ("GHSA", "GO", "PYSEC")]


# --- Containerfiles and base images -----------------------------------------


def bundle_images(containerfile: str) -> dict[str, str]:
    """ARG FOO_IMAGE="registry/ns/name@sha256:..." -> {FOO_IMAGE: ref}."""
    return dict(
        re.findall(r'^ARG\s+(\w+_IMAGE)="?([^"\s]+)"?', containerfile, re.M)
    )


def image_name(ref: str) -> str:
    """registry.redhat.io/ns/mtv-api-rhel9@sha256:abc -> mtv-api-rhel9."""
    return re.split(r"[@:]", ref.rsplit("/", 1)[-1], maxsplit=1)[0]


def final_from(containerfile: str) -> str:
    refs = re.findall(r"^FROM\s+(\S+)", containerfile, re.M | re.I)
    return refs[-1] if refs else ""


def base_component(ref: str) -> str:
    """registry.redhat.io/openshift4/ose-cli-rhel9@sha256:.. -> openshift4/ose-cli-rhel9."""
    path = re.split(r"@|:(?=[^/]*$)", ref, maxsplit=1)[0]
    parts = path.split("/")
    if "." in parts[0]:
        parts = parts[1:]
    return "/".join(parts)


def stream_from_version(version_label: str) -> str:
    m = re.match(r"v?(\d+\.\d+)", version_label or "")
    return m.group(1) if m else ""


def tracker_streams(issues: list[dict]) -> list[str]:
    """'... [openshift-4.16.z]' -> '4.16.z', for every tracker returned."""
    streams = {
        m.group(1)
        for issue in issues
        if (
            m := re.search(
                r"\[openshift-([\d.]+(?:\.z)?)\]",
                issue.get("fields", {}).get("summary", ""),
            )
        )
    }
    return sorted(streams, key=lambda s: parse_version(s.removesuffix(".z")))


def pick_ocp_tracker(issues: list[dict], stream: str) -> OcpTracker | None:
    pattern = re.compile(rf"\[openshift-{re.escape(stream)}(?:\.z)?\]")
    for issue in issues:
        fields = issue.get("fields", {})
        if pattern.search(fields.get("summary", "")):
            return OcpTracker(
                key=issue["key"],
                status=(fields.get("status") or {}).get("name", ""),
                resolution=(fields.get("resolution") or {}).get("name", ""),
            )
    return None


# --- Image and lockfile scanning ----------------------------------------------


def parse_go_buildinfo(data: bytes) -> dict[str, str]:
    """Module -> version from the buildinfo Go embeds in every binary."""
    modules: dict[str, str] = {}
    last = None
    for kind, mod, ver in _GO_DEP_LINE.findall(data):
        if kind == b"dep":
            last = mod.decode(errors="replace")
            modules[last] = ver.decode(errors="replace")
        elif last:
            # "=>" replace directive applies to the preceding dep
            modules[last] = ver.decode(errors="replace")
    return modules


def scan_image_dir(
    image_dir: str,
    prefixes: list[str],
    base_layers: set[str],
    needles: set[str],
) -> tuple[dict, dict]:
    """Walk a `skopeo copy dir:` layout, later layers overriding earlier ones.

    Returns (go, python):
      go:     {path: (modules, from_base, needles_present)}
      python: {name: (version, path, from_base)}
    """
    with open(os.path.join(image_dir, "manifest.json")) as f:
        layers = [layer["digest"] for layer in json.load(f)["layers"]]

    go: dict[str, tuple] = {}
    python: dict[str, tuple] = {}
    for digest in layers:
        from_base = digest in base_layers
        blob = os.path.join(image_dir, digest.split(":", 1)[1])
        with tarfile.open(blob, "r:*") as tar:
            for member in tar:
                path = member.name.lstrip("./")
                base_name = os.path.basename(path)
                if base_name.startswith(".wh."):
                    removed = os.path.join(
                        os.path.dirname(path), base_name[4:]
                    )
                    go.pop(removed, None)
                    continue
                if not member.isfile() or not path.startswith(
                    tuple(prefixes)
                ):
                    continue
                dist = _DIST_INFO.search(path)
                if dist:
                    name, _, version = dist.group(1).rpartition("-")
                    python[normalize_name(name)] = (version, path, from_base)
                    continue
                fobj = tar.extractfile(member)
                if fobj is None or fobj.read(4) != b"\x7fELF":
                    continue
                data = fobj.read()
                if _GO_BUILDINFO_MAGIC not in data:
                    continue
                present = {n for n in needles if n.encode() in data}
                go[path] = (parse_go_buildinfo(data), from_base, present)
    return go, python


def parse_package_lock(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for key, meta in json.loads(text).get("packages", {}).items():
        if "node_modules/" not in key or "version" not in meta:
            continue
        name = key.rsplit("node_modules/", 1)[1]
        found.setdefault(name, set()).add(meta["version"])
    return found


def parse_yarn_lock(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    names: set[str] = set()
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            names = {
                spec.strip().strip('"').rsplit("@", 1)[0]
                for spec in line.rstrip()[:-1].split(",")
            }
            continue
        m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?', line)
        if m and names:
            for name in names:
                found.setdefault(name, set()).add(m.group(1))
            names = set()
    return found


def found_packages(
    candidates: list[str],
    go: dict,
    python: dict,
    npm: dict[str, set[str]],
    npm_location: str,
    needles: dict[str, list[str]],
) -> list[FoundPackage]:
    wanted = {normalize_name(c) for c in candidates}
    result: list[FoundPackage] = []
    for path, (modules, from_base, present) in go.items():
        for mod, ver in modules.items():
            if normalize_name(mod) not in wanted:
                continue
            pkg_needles = set(needles.get(normalize_name(mod), []))
            result.append(
                FoundPackage(
                    name=mod,
                    version=ver,
                    location=f"/{path}",
                    from_base=from_base,
                    paths_present=bool(pkg_needles & present)
                    if pkg_needles
                    else None,
                )
            )
    for name, (ver, path, from_base) in python.items():
        if name in wanted:
            result.append(
                FoundPackage(
                    name=name,
                    version=ver,
                    location=f"/{os.path.dirname(path)}",
                    from_base=from_base,
                )
            )
    for name, versions in npm.items():
        if normalize_name(name) in wanted:
            result.extend(
                FoundPackage(name=name, version=v, location=npm_location)
                for v in sorted(versions)
            )
    return result


# --- Classification -----------------------------------------------------------


def _describe(p: FoundPackage) -> str:
    origin = " (inherited from the base image)" if p.from_base else ""
    return f"{p.location}: {p.name} {p.version}{origin}"


def _fmt(v: tuple | None) -> str:
    return ".".join(str(x) for x in v) if v else "?"


def _describe_ranges(ranges: list[VulnRange]) -> str:
    parts = []
    for r in ranges:
        lo = f">= {_fmt(r.introduced)}, " if r.introduced != (0,) else ""
        if r.fixed:
            parts.append(f"{lo}< {_fmt(r.fixed)}")
        elif r.last_affected:
            parts.append(f"{lo}<= {_fmt(r.last_affected)}")
    return "; ".join(parts) or "unknown"


def classify(facts: Facts, jira_url: str) -> tuple[str, list[str]]:
    t = facts.ticket
    if t.embargoed:
        return SKIPPED, ["Embargoed; left for manual / ProdSec handling."]
    if facts.not_built_reason:
        return NOT_BUILT, [facts.not_built_reason]
    if facts.error:
        return NEEDS_REVIEW, [facts.error]
    if not facts.candidates:
        return NEEDS_REVIEW, ["Could not determine the affected package."]
    if not facts.found:
        return NEEDS_REVIEW, [
            f"None of {', '.join(facts.candidates)} found in the scanned Go "
            "binaries, Python packages or lockfile; it may come from an RPM "
            "or a path that isn't scanned."
        ]

    evidence = [_describe(p) for p in facts.found]
    if facts.ranges:
        evidence.append(f"Vulnerable range: {_describe_ranges(facts.ranges)}")
        vulnerable = [
            p for p in facts.found if is_vulnerable(p.version, facts.ranges)
        ]
        if not vulnerable:
            if all(
                predates_ranges(p.version, facts.ranges) for p in facts.found
            ):
                return NOT_AFFECTED_VERSION, evidence
            return FIXED_IN_BUILD, evidence
    else:
        # No fixed version known: treat every copy as potentially vulnerable
        # and rely on the version-independent checks below.
        vulnerable = facts.found

    if all(p.paths_present is False for p in vulnerable):
        return NOT_AFFECTED_CODE_ABSENT, evidence + [
            "The vulnerable package paths are not linked into the binary."
        ]

    if any(not p.from_base for p in vulnerable):
        if not facts.ranges:
            return NEEDS_REVIEW, evidence + [
                "No fixed version known for this CVE."
            ]
        return AFFECTED, evidence

    base = facts.base
    if base is None or not base.component.startswith("openshift4/"):
        return NEEDS_REVIEW, evidence + [
            f"Inherited from base image {base.ref if base else '(unknown)'}, "
            "which isn't an OpenShift image with an OCPBUGS tracker."
        ]
    evidence.append(f"Base image: {base.ref} (OpenShift {base.stream})")
    if facts.ocp is None:
        if facts.ocp_streams is None:
            searched = "The OCPBUGS lookup failed."
        elif not facts.ocp_streams:
            searched = (
                "The OCPBUGS search returned no trackers (possibly a "
                "transient Jira search issue; retried next run)."
            )
        else:
            searched = (
                "The OCPBUGS search returned trackers for "
                f"{', '.join(facts.ocp_streams)} only."
            )
        return NEEDS_REVIEW, evidence + [
            f"No OCPBUGS tracker found for {t.cve} on {base.component} "
            f"[openshift-{base.stream}]. {searched}"
        ]
    ocp = facts.ocp
    evidence.append(
        f"OpenShift tracker {jira_url}browse/{ocp.key}: {ocp.status}"
        + (f" / {ocp.resolution}" if ocp.resolution else "")
    )
    if ocp.resolution == "Not a Bug":
        return NOT_AFFECTED_OCP, evidence
    if not facts.ranges:
        return NEEDS_REVIEW, evidence + ["No fixed version known for this CVE."]
    if ocp.resolution in OCP_FIXED_RESOLUTIONS:
        return BASE_BUMP_NEEDED, evidence
    return BLOCKED_ON_OCP, evidence


# --- Rendering ---------------------------------------------------------------

_COMMENT_TEXT = {
    FIXED_IN_BUILD: (
        "fixed in the built image.",
        "Suggested next step: move to ASSIGNED and target the next {branch} "
        "z-stream.",
    ),
    NOT_AFFECTED_VERSION: (
        "not affected; the shipped version is outside the vulnerable range.",
        "Proposed resolution: Not a Bug (component not affected).",
    ),
    NOT_AFFECTED_CODE_ABSENT: (
        "vulnerable code not present in the binary.",
        "Proposed resolution: Not a Bug (vulnerable code not present).",
    ),
    NOT_AFFECTED_OCP: (
        "inherited from the OpenShift base image; OpenShift closed the same "
        "CVE as Not a Bug.",
        "Proposed resolution: Not a Bug, following OpenShift's assessment.",
    ),
    NOT_BUILT: (
        "this image variant is not built on {branch}.",
        "Proposed resolution: Not a Bug (component not shipped).",
    ),
    BLOCKED_ON_OCP: (
        "blocked on OpenShift; the vulnerable package is inherited from the "
        "base image.",
        "Next step: bump the base image pin once OpenShift ships a fixed "
        "build.",
    ),
    BASE_BUMP_NEEDED: (
        "OpenShift has fixed this; the base image pin needs a bump.",
        "Next step: bump the base image to a build that includes the fix.",
    ),
    AFFECTED: (
        "affected; the vulnerable version is built by MTV.",
        "Next step: bump the dependency on {branch}.",
    ),
}


def render_comment(
    category: str, ticket: CveTicketDTO, image: str, evidence: list[str]
) -> str:
    headline, next_step = _COMMENT_TEXT[category]
    lines = [
        f"*Assessment: {headline.format(branch=ticket.branch)}*",
        "",
        f"Component {{{{{ticket.component}}}}} on {ticket.branch}"
        + (f", image {{{{{image}}}}}" if image else "")
        + ":",
    ]
    lines += [f"* {e}" for e in evidence]
    lines += [
        "",
        next_step.format(branch=ticket.branch),
        "",
        f"_{COMMENT_MARKER.format(category=category)} posted by the "
        "mtv-releng cve_triage pipeline_",
    ]
    return "\n".join(lines)


def render_report(findings, jira_url: str) -> str:
    lines = ["# CVE triage report", ""]
    order = list(ACTIONABLE) + [NEEDS_REVIEW, SKIPPED]
    for category in order:
        group = [f for f in findings if f.category == category]
        if not group:
            continue
        lines += [f"## {category} ({len(group)})", ""]
        lines += ["| Ticket | Branch | Component | CVE | Due | Evidence |"]
        lines += ["|---|---|---|---|---|---|"]
        for f in group:
            t = f.ticket
            evidence = "<br>".join(e.replace("|", "\\|") for e in f.evidence)
            lines.append(
                f"| [{t.key}]({jira_url}browse/{t.key}) | {t.branch} | "
                f"{t.component} | {t.cve} | {t.due_date} | {evidence} |"
            )
        lines.append("")
    return "\n".join(lines)
