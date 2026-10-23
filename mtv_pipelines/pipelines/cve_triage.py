import argparse
import datetime
import json
import logging
import os
import tarfile
from argparse import ArgumentParser, Namespace
from asyncio import TaskGroup, to_thread

import requests
from config import config
from core.task import depends_on, task
from models.dto import (
    CveFindingDTO,
    CveFindingsDTO,
    CveTicketDTO,
    CveTicketsDTO,
    CveTriageResultDTO,
    EmptyDTO,
)
from semver import Version
from tasks.cve_triage import (
    ACTIONABLE,
    AFFECTED,
    BASE_BUMP_NEEDED,
    COMMENT_MARKER,
    NEEDS_REVIEW,
    BaseImage,
    Facts,
    OcpTracker,
    base_component,
    bundle_images,
    candidates_from_title,
    classify,
    final_from,
    found_packages,
    image_name,
    normalize_name,
    osv_packages,
    osv_related_ids,
    parse_package_lock,
    parse_ticket,
    parse_yarn_lock,
    pick_ocp_tracker,
    tracker_streams,
    ranges_from_text,
    render_comment,
    render_report,
    scan_image_dir,
    stream_from_version,
)
from utils import create_temp_dir, replace_for_quay
from wrappers import osv
from wrappers.osv import OsvLookupError
from wrappers.jira import Jira, JiraForbiddenError
from wrappers.skopeo import Skopeo
from wrappers.slack import Slack, SlackBuilder

DESCRIPTION = (
    "Weekly triage of open MTV SecurityTracking Jira tickets.\n\n"
    "For each ticket, resolves the image its branch's operator bundle ships,\n"
    "reads the Go buildinfo / Python packages (or the console plugin lockfile)\n"
    "to find the shipped version of the affected package, and for packages\n"
    "inherited from an OpenShift base image looks up OpenShift's OCPBUGS\n"
    "tracker for the same CVE and stream. Writes a report and Slack summary.\n"
    "Dry-run by default; pass --apply to post the assessment comment and a\n"
    "triage:* label. Status changes are always left to a human."
)

logger = logging.getLogger(__name__)

TICKET_FIELDS = ["summary", "labels", "duedate", "status", "description"]
OCP_FIELDS = ["summary", "status", "resolution"]
NEEDS_HUMAN = (AFFECTED, BASE_BUMP_NEEDED, NEEDS_REVIEW)


def arg_parse(arg_parser: ArgumentParser):
    arg_parser.formatter_class = argparse.RawTextHelpFormatter
    arg_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="[optional] Post comments and labels. Without it, dry-run only.",
    )
    arg_parser.add_argument(
        "--jql",
        default="",
        help="[optional] Override the ticket query from config.",
    )
    arg_parser.add_argument(
        "--keys",
        default="",
        help="[optional] Comma-separated ticket keys, e.g. MTV-1,MTV-2. "
        "Also re-triages tickets that already carry a triage:* label.",
    )
    arg_parser.add_argument(
        "--branches",
        default="",
        help="[optional] Comma-separated branches, e.g. release-2.10.",
    )
    arg_parser.add_argument(
        "--skip-slack",
        dest="skip_slack",
        action="store_true",
        default=False,
        help="[optional] Don't post the Slack summary.",
    )
    arg_parser.add_argument(
        "--report-dir",
        dest="report_dir",
        default="",
        help="[optional] Report output directory (default from config).",
    )


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _fetch_raw(repo: str, branch: str, path: str) -> str | None:
    url = f"{config.get_cve_triage()['raw_base_url']}/{repo}/{branch}/{path}"
    try:
        resp = requests.get(url, timeout=config.get_timeouts()["http_seconds"])
    except requests.RequestException as e:
        logger.warning(f"{url} failed: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"{url} returned {resp.status_code}")
        return None
    return resp.text


@task
async def collect_tickets(
    data: EmptyDTO, args: Namespace, tg: TaskGroup
) -> CveTicketsDTO:
    keys = _split(args.keys)
    jql = (
        f"key in ({', '.join(keys)})"
        if keys
        else args.jql or config.get_cve_triage()["jql"]
    )
    try:
        issues = Jira().search(jql, TICKET_FIELDS)
    except ValueError as e:
        return CveTicketsDTO(
            skipped=True, skip_reason=f"Jira credentials missing: {e}"
        )
    except (JiraForbiddenError, requests.RequestException) as e:
        return CveTicketsDTO(skipped=True, skip_reason=f"Jira search: {e}")

    branches = _split(args.branches)
    tickets = []
    for ticket in (parse_ticket(i) for i in issues):
        if branches and ticket.branch not in branches:
            continue
        if not keys and any(lbl.startswith("triage:") for lbl in ticket.labels):
            logger.info(f"{ticket.key} already triaged; skipping")
            continue
        tickets.append(ticket)
    logger.info(f"Collected {len(tickets)} tickets from: {jql}")
    return CveTicketsDTO(tickets=tickets)


def _vuln_info(ticket: CveTicketDTO, aliases: dict) -> tuple:
    """(candidates, {name: ranges}, {name: needles}) for one ticket."""
    records = []
    record = osv.get_vuln(ticket.cve) if ticket.cve else None
    if record:
        records.append(record)
        records += [
            r for r in map(osv.get_vuln, osv_related_ids(record)) if r
        ]
    ranges, imports = osv_packages(records)
    candidates = list(ranges) + [
        normalize_name(c)
        for c in candidates_from_title(ticket.summary, aliases)
        if normalize_name(c) not in ranges
    ]
    # Curated paths win over OSV's import list, which often includes shared
    # packages (e.g. grpc/internal/transport) that every binary links.
    curated = config.get_cve_triage().get("vulnerable_paths", {}).get(
        ticket.cve, []
    )
    needles = {c: curated or imports.get(c, []) for c in candidates}
    return candidates, ranges, needles


def _inspect_base(containerfile: str) -> BaseImage:
    """Final-stage base image; raises if its layers can't be read.

    Without the base layer digests every file would look MTV-built, so a
    failure here must stop the scan rather than misattribute packages.
    """
    ref = final_from(containerfile)
    if not ref:
        raise RuntimeError("Containerfile has no FROM line")
    try:
        info = Skopeo().inspect(ref, arch=config.get_cve_triage()["arch"])
    except RuntimeError as e:
        raise RuntimeError(f"Could not inspect base image {ref}: {e}") from e
    layers = set(info.get("Layers", []))
    if not layers:
        raise RuntimeError(f"Base image {ref} reported no layers")
    return BaseImage(
        ref=ref,
        component=base_component(ref),
        stream=stream_from_version(info.get("Labels", {}).get("version")),
        layer_digests=layers,
    )


def _scan_component(
    branch: str, component: str, bundle: dict, needles: set[str]
) -> dict:
    """Image facts shared by every ticket on one branch + component.

    Runs in a TaskGroup thread, where an uncaught exception would cancel
    every other scan and lose the whole run; failures become an error that
    classifies the tickets as NEEDS_REVIEW instead.
    """
    try:
        return _scan_component_unguarded(branch, component, bundle, needles)
    except (
        RuntimeError,
        OSError,
        KeyError,
        ValueError,
        tarfile.TarError,
        requests.RequestException,
    ) as e:
        logger.warning(f"Scan of {component} on {branch} failed: {e}")
        return {"error": f"Could not scan {component} on {branch}: {e}"}


def _scan_component_unguarded(
    branch: str, component: str, bundle: dict, needles: set[str]
) -> dict:
    ct = config.get_cve_triage()
    cmp = config.get_cmp_mappings().get(component)
    if not cmp or not cmp.get("arg"):
        return {"error": f"Unknown component {component} (not in cmp_mappings)"}
    ref = bundle.get(cmp["arg"])
    if not ref:
        return {
            "not_built": f"The {branch} bundle has no {cmp['arg']}; "
            f"{component} is not shipped on this branch."
        }
    if image_name(ref) != component:
        return {
            "not_built": f"The {branch} bundle ships {image_name(ref)}, "
            f"not {component}."
        }

    major, minor = branch.removeprefix("release-").split(".")
    image = replace_for_quay(ref, Version(int(major), int(minor), 0))
    repo = ct["repos"][cmp["origin"]]
    result = {"image": image, "go": {}, "python": {}, "npm": {}}

    for lockfile in ct.get("lockfiles", {}).get(component, []):
        text = _fetch_raw(repo, branch, lockfile)
        if text is None:
            continue
        result["npm"] = (
            parse_package_lock(text)
            if lockfile.endswith(".json")
            else parse_yarn_lock(text)
        )
        result["npm_location"] = f"{repo}@{branch}:{lockfile}"
        return result

    path = ct.get("containerfiles", {}).get(
        component, f"build/{cmp['upstream']}/Containerfile-downstream"
    )
    containerfile = _fetch_raw(repo, branch, path)
    if containerfile is None:
        return {
            **result,
            "error": f"Could not fetch {repo}@{branch}:{path}, so inherited "
            "packages can't be told apart from MTV-built ones",
        }
    base = _inspect_base(containerfile)
    result["base"] = base

    tmp = create_temp_dir(f"cve-triage-{component}")
    try:
        Skopeo().copy(image, tmp.name, arch=ct["arch"])
        result["go"], result["python"] = scan_image_dir(
            tmp.name, ct["scan_prefixes"], base.layer_digests, needles
        )
    finally:
        tmp.cleanup()
    return result


@task
@depends_on(collect_tickets)
async def analyze(
    data: CveTicketsDTO, args: Namespace, tg: TaskGroup
) -> CveFindingsDTO:
    if data.skipped:
        logger.warning(data.skip_reason)
        return CveFindingsDTO(findings=[])

    ct = config.get_cve_triage()
    jira_url = config.get_jira_url()
    try:
        Skopeo().auth_prod()
    except RuntimeError as e:
        # Not fatal here: base inspections that then fail are reported as
        # NEEDS_REVIEW per component.
        logger.warning(f"registry.redhat.io login failed: {e}")

    # OSV lookups are per CVE; image scans are per branch + component.
    vuln = {}
    osv_errors: dict[str, str] = {}
    for t in data.tickets:
        if t.embargoed or not t.cve or t.cve in vuln or t.cve in osv_errors:
            continue
        try:
            vuln[t.cve] = _vuln_info(t, ct["package_aliases"])
        except OsvLookupError as e:
            # Don't fall back to the flaw text: the ticket would get a
            # permanent label based on weaker evidence than a retry gives.
            logger.warning(str(e))
            osv_errors[t.cve] = str(e)

    groups: dict[tuple, set[str]] = {}
    for t in data.tickets:
        if t.embargoed or not t.branch or not t.component:
            continue
        needles = groups.setdefault((t.branch, t.component), set())
        for paths in vuln.get(t.cve, ([], {}, {}))[2].values():
            needles.update(paths)

    bundles = {}
    for branch in {b for b, _ in groups}:
        text = _fetch_raw(ct["bundle_repo"], branch, ct["bundle_containerfile"])
        bundles[branch] = bundle_images(text) if text else None

    scans = {}
    for (branch, component), needles in groups.items():
        if bundles[branch] is None:
            scans[(branch, component)] = {
                "error": f"No operator bundle found on {branch}"
            }
            continue
        scans[(branch, component)] = tg.create_task(
            to_thread(
                _scan_component, branch, component, bundles[branch], needles
            )
        )
    for key, value in scans.items():
        if not isinstance(value, dict):
            scans[key] = await value

    jira = Jira()
    ocp_cache: dict[tuple, list[dict] | None] = {}
    findings = []
    for t in data.tickets:
        facts = Facts(ticket=t)
        if not t.embargoed:
            scan = scans.get((t.branch, t.component))
            if scan is None:
                facts.error = "Ticket has no [mtv-X.Y] branch or pscomponent."
            elif t.cve in osv_errors:
                # NOT_BUILT doesn't depend on OSV, so it still applies.
                facts.not_built_reason = scan.get("not_built", "")
                facts.error = f"{osv_errors[t.cve]}; will retry next run."
            elif t.cve not in vuln:
                facts.error = "Ticket has no CVE-* label."
            else:
                facts.image = scan.get("image", "")
                facts.not_built_reason = scan.get("not_built", "")
                facts.error = scan.get("error", "")
                facts.base = scan.get("base")
                candidates, ranges, needles = vuln[t.cve]
                facts.candidates = candidates
                facts.found = found_packages(
                    candidates,
                    scan.get("go", {}),
                    scan.get("python", {}),
                    scan.get("npm", {}),
                    scan.get("npm_location", ""),
                    needles,
                )
                names = {normalize_name(p.name) for p in facts.found}
                facts.ranges = [
                    r for n in names for r in ranges.get(n, [])
                ] or ranges_from_text(t.description)

                base = facts.base
                if (
                    base
                    and base.stream
                    and base.component.startswith("openshift4/")
                    and any(p.from_base for p in facts.found)
                ):
                    facts.ocp, facts.ocp_streams = _lookup_ocp(
                        jira, ocp_cache, t.cve, base
                    )

        category, evidence = classify(facts, jira_url)
        logger.info(f"{t.key}: {category}")
        findings.append(
            CveFindingDTO(
                ticket=t,
                category=category,
                evidence=evidence,
                image=facts.image,
            )
        )
    return CveFindingsDTO(findings=findings)


def _lookup_ocp(
    jira: Jira, cache: dict, cve: str, base: BaseImage
) -> tuple[OcpTracker | None, list[str] | None]:
    """(tracker for *base*'s stream, streams the search returned).

    The streams are None when the lookup failed. A failure leaves the ticket
    at NEEDS_REVIEW instead of aborting the run, and is cached so the same
    CVE isn't retried for every ticket.
    """
    key = (cve, base.component)
    if key not in cache:
        try:
            cache[key] = jira.search(
                "project = OCPBUGS AND labels = SecurityTracking "
                f'AND labels = "{cve}" '
                f'AND labels = "pscomponent:{base.component}"',
                OCP_FIELDS,
            )
        except (JiraForbiddenError, requests.RequestException) as e:
            logger.warning(f"OCPBUGS lookup for {cve} failed: {e}")
            cache[key] = None
    issues = cache[key]
    if issues is None:
        return None, None
    return pick_ocp_tracker(issues, base.stream), tracker_streams(issues)


def _apply(jira: Jira, finding: CveFindingDTO) -> bool:
    """Comment (once) and label; True when a new comment was posted.

    The label is added even when the comment already exists, so a run that
    commented but failed to label is completed by the next one.
    """
    key = finding.ticket.key
    marker = COMMENT_MARKER.split("{", 1)[0]
    commented = any(marker in body for body in jira.get_comments(key))
    if commented:
        logger.info(f"{key} already has a cve-triage comment; labelling only")
    else:
        jira.add_comment(key, finding.comment)
    jira.add_labels(key, [finding.label])
    return not commented


def _send_slack(
    findings: list[CveFindingDTO], counts: dict, report: str
) -> None:
    # Counts and Jira links only: the links need Jira access, and nothing in
    # the message says which branch or component ships what.
    channel = config.get_cve_triage().get("slack_channel")
    if not channel:
        logger.warning(
            "cve_triage.slack_channel is not set; not posting the summary "
            "(it must be a private channel)"
        )
        return
    jira_url = config.get_jira_url()
    builder = SlackBuilder()
    builder.header(f"CVE triage | {datetime.date.today().isoformat()}")
    builder.section(
        "\n".join(f"*{cat}*  {n}" for cat, n in sorted(counts.items()))
    )
    attention = [f for f in findings if f.category in NEEDS_HUMAN]
    if attention:
        builder.section(
            "*Needs a human:* "
            + ", ".join(
                f"<{jira_url}browse/{f.ticket.key}|{f.ticket.key}>"
                for f in attention[:25]
            )
        )
    builder.context([f"Report: `{report}`"])
    Slack(channel).send_block(builder.build(), channel)


@task
@depends_on(analyze)
async def act_and_report(
    data: CveFindingsDTO, args: Namespace, tg: TaskGroup
) -> CveTriageResultDTO:
    ct = config.get_cve_triage()
    dry_run = not args.apply
    jira = None if dry_run else Jira()

    applied = 0
    for f in data.findings:
        if f.category not in ACTIONABLE:
            continue
        f.label = ct["labels"][f.category]
        f.comment = render_comment(f.category, f.ticket, f.image, f.evidence)
        if dry_run:
            # Comment bodies and evidence only go to the report file: they
            # describe unfixed vulnerabilities on non-public trackers.
            logger.info(
                f"DRY RUN: would comment on {f.ticket.key} and add label "
                f"{f.label}"
            )
            continue
        try:
            f.applied = _apply(jira, f)
            applied += f.applied
        except (JiraForbiddenError, requests.RequestException) as e:
            logger.error(f"Failed to update {f.ticket.key}: {e}")

    counts: dict[str, int] = {}
    for f in data.findings:
        counts[f.category] = counts.get(f.category, 0) + 1

    report_dir = args.report_dir or ct["report_dir"]
    os.makedirs(report_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report = os.path.join(report_dir, f"report-{stamp}.md")
    with open(report, "w") as fh:
        fh.write(render_report(data.findings, config.get_jira_url()))
    with open(report.removesuffix(".md") + ".json", "w") as fh:
        json.dump([f.model_dump() for f in data.findings], fh, indent=2)
    logger.info(
        {
            "msg": "CVE triage summary",
            "counts": counts,
            "report": report,
            "applied": applied,
        }
    )

    if not args.skip_slack and data.findings:
        try:
            _send_slack(data.findings, counts, report)
        except Exception as e:
            logger.error(f"Slack summary failed: {e}")

    return CveTriageResultDTO(
        counts=counts, report_path=report, applied=applied, dry_run=dry_run
    )
