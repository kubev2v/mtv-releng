"""Tests for the cve_triage pipeline helpers.

Classification cases mirror tickets triaged by hand on 2026-10-15.
"""

import asyncio
import gzip
import io
import json
import os
import tarfile
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest
from models.dto import CveFindingDTO, CveFindingsDTO, CveTicketDTO
from tasks import cve_triage as ct
from tasks.cve_triage import BaseImage, Facts, FoundPackage, OcpTracker

from pipelines import cve_triage as pipeline

JIRA = "https://redhat.atlassian.net/"


def run_task(task, data, args):
    async def _inner():
        async with asyncio.TaskGroup() as tg:
            return await task.run(data, args, tg)

    return asyncio.run(_inner())


def _ticket(**kwargs) -> CveTicketDTO:
    values = {
        "key": "MTV-1",
        "summary": "CVE-1 x/mtv-api-rhel9: lib: bug [mtv-2.10]",
        "branch": "release-2.10",
        "cve": "CVE-2026-1",
        "component": "mtv-api-rhel9",
    }
    values.update(kwargs)
    return CveTicketDTO(**values)


# --- ticket parsing -----------------------------------------------------------


def test_parse_ticket_extracts_branch_cve_component_and_adf_text():
    issue = {
        "key": "MTV-6839",
        "fields": {
            "summary": "CVE-2026-84445 migration-toolkit-virtualization/"
            "mtv-validation-rhel9: gRPC-Go: DoS [mtv-2.10]",
            "labels": [
                "CVE-2026-84445",
                "SecurityTracking",
                "pscomponent:migration-toolkit-virtualization/"
                "mtv-validation-rhel9",
            ],
            "duedate": "2026-10-14",
            "status": {"name": "New"},
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Prior to 1.82.2"}],
                    }
                ],
            },
        },
    }
    t = ct.parse_ticket(issue)
    assert t.branch == "release-2.10"
    assert t.cve == "CVE-2026-84445"
    assert t.component == "mtv-validation-rhel9"
    assert t.description.strip() == "Prior to 1.82.2"
    assert not t.embargoed


def test_parse_ticket_flags_embargoed():
    issue = {
        "key": "MTV-6789",
        "fields": {"summary": "EMBARGOED CVE-2026-84503 x: y [mtv-2.10]"},
    }
    assert ct.parse_ticket(issue).embargoed


def test_candidates_from_title_uses_aliases():
    aliases = {"grpc-go": ["google.golang.org/grpc"], "billy": ["x/billy"]}
    summary = "CVE-1 ns/mtv-validation-rhel9: gRPC-Go: DoS [mtv-2.11]"
    assert ct.candidates_from_title(summary, aliases) == [
        "google.golang.org/grpc"
    ]


# --- versions -------------------------------------------------------------------


def test_parse_version_handles_prefix_and_suffix():
    assert ct.parse_version("v1.82.2") == (1, 82, 2)
    assert ct.parse_version("27.1.1+incompatible") == (27, 1, 1)
    assert ct.parse_version("v0.0.0-20260831172235-160ac2acaa79") == (0, 0, 0)


def test_ranges_from_text_parallel_release_lines():
    ranges = ct.ranges_from_text(
        "Prior to 1.82.2 and 1.83.2, servers created with ..."
    )
    assert ct.is_vulnerable("v1.77.0", ranges)
    assert not ct.is_vulnerable("v1.82.2", ranges)
    assert ct.is_vulnerable("v1.83.1", ranges)
    assert not ct.is_vulnerable("v1.83.2", ranges)


def test_ranges_from_text_from_until():
    ranges = ct.ranges_from_text("From 1.26.0 until 2.8.0, the proxy ...")
    assert ct.is_vulnerable("2.6.3", ranges)
    assert not ct.is_vulnerable("2.8.0", ranges)
    assert not ct.is_vulnerable("1.25.0", ranges)


def test_ranges_from_text_through_is_inclusive_and_predates():
    ranges = ct.ranges_from_text("In versions 7.7.0 through 7.13.1, when ...")
    assert ct.is_vulnerable("7.13.1", ranges)
    assert not ct.is_vulnerable("7.13.2", ranges)
    assert ct.predates_ranges("5.3.4", ranges)


def test_osv_packages_reads_ranges_and_imports():
    record = {
        "affected": [
            {
                "package": {"ecosystem": "Go", "name": "google.golang.org/grpc"},
                "ranges": [
                    {
                        "type": "SEMVER",
                        "events": [
                            {"introduced": "0"},
                            {"fixed": "1.82.2"},
                            {"introduced": "1.83.0"},
                            {"fixed": "1.83.2"},
                        ],
                    }
                ],
                "ecosystem_specific": {
                    "imports": [{"path": "google.golang.org/grpc/xds/server"}]
                },
            },
            {"package": {"ecosystem": "Go", "name": "stdlib"}, "ranges": []},
        ]
    }
    ranges, imports = ct.osv_packages([record])
    assert list(ranges) == ["google.golang.org/grpc"]
    assert len(ranges["google.golang.org/grpc"]) == 2
    assert imports == {
        "google.golang.org/grpc": ["google.golang.org/grpc/xds/server"]
    }


# --- containerfiles / base images ---------------------------------------------


def test_bundle_images_and_image_name():
    cf = (
        'ARG API_IMAGE="registry.redhat.io/migration-toolkit-virtualization/'
        'mtv-api-rhel9@sha256:abc"\n'
        'ARG MUST_GATHER_IMAGE="registry.redhat.io/ns/'
        'mtv-must-gather-rhel9@sha256:def"\n'
    )
    images = ct.bundle_images(cf)
    assert ct.image_name(images["API_IMAGE"]) == "mtv-api-rhel9"
    assert ct.image_name(images["MUST_GATHER_IMAGE"]) == "mtv-must-gather-rhel9"


def test_final_from_and_base_component():
    cf = (
        "FROM registry.redhat.io/ubi9/go-toolset:1.25 AS builder\n"
        "FROM registry.redhat.io/openshift4/ose-cli-rhel9@sha256:5227\n"
    )
    ref = ct.final_from(cf)
    assert ref.endswith("@sha256:5227")
    assert ct.base_component(ref) == "openshift4/ose-cli-rhel9"
    assert (
        ct.base_component(
            "registry.redhat.io/openshift4/ose-ansible-rhel9-operator:v4.18.0"
        )
        == "openshift4/ose-ansible-rhel9-operator"
    )
    assert ct.stream_from_version("v4.16.0") == "4.16"


def test_pick_ocp_tracker_matches_stream_including_z():
    issues = [
        {
            "key": "OCPBUGS-1",
            "fields": {"summary": "CVE-1 openshift4/ose-cli-rhel9: x [openshift-4.18]"},
        },
        {
            "key": "OCPBUGS-2",
            "fields": {
                "summary": "CVE-1 openshift4/ose-cli-rhel9: x [openshift-4.16.z]",
                "status": {"name": "Closed"},
                "resolution": {"name": "Not a Bug"},
            },
        },
    ]
    tracker = ct.pick_ocp_tracker(issues, "4.16")
    assert tracker == OcpTracker("OCPBUGS-2", "Closed", "Not a Bug")
    assert ct.pick_ocp_tracker(issues, "4.20") is None


# --- image / lockfile scanning --------------------------------------------------

GO_BINARY = (
    b"\x7fELF....\xff Go buildinf:...."
    b"path\tgithub.com/openshift/oc\n"
    b"dep\tgithub.com/go-git/go-git/v5\tv5.13.1\th1:abc=\n"
    b"dep\tgo.mongodb.org/mongo-driver\tv1.14.0\th1:def=\n"
    b"dep\tgithub.com/foo/bar\tv1.0.0\n"
    b"=>\tgithub.com/foo/bar-fork\tv1.0.1\th1:ghi=\n"
    b"...github.com/moby/buildkit/frontend/dockerfile..."
)


def test_parse_go_buildinfo_applies_replacements():
    mods = ct.parse_go_buildinfo(GO_BINARY)
    assert mods["github.com/go-git/go-git/v5"] == "v5.13.1"
    assert mods["github.com/foo/bar"] == "v1.0.1"


def _layer(path, files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    blob = gzip.compress(buf.getvalue())
    digest = f"sha256:{abs(hash(blob)):064x}"[:71]
    with open(os.path.join(path, digest.split(":")[1]), "wb") as f:
        f.write(blob)
    return digest


def test_scan_image_dir_marks_base_layers_and_needles(tmp_path):
    base = _layer(tmp_path, {"usr/bin/oc": GO_BINARY})
    mtv = _layer(
        tmp_path,
        {
            "usr/local/lib/python3.12/site-packages/"
            "urllib3-2.6.3.dist-info/METADATA": b"x",
            "usr/share/doc/ignored": b"x",
        },
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"layers": [{"digest": base}, {"digest": mtv}]})
    )
    go, python = ct.scan_image_dir(
        str(tmp_path),
        ["usr/bin/", "usr/local/lib/"],
        {base},
        {"go.mongodb.org/mongo-driver/mongo/gridfs", "moby/buildkit/frontend"},
    )
    mods, from_base, present = go["usr/bin/oc"]
    assert mods["go.mongodb.org/mongo-driver"] == "v1.14.0"
    assert from_base is True
    assert present == {"moby/buildkit/frontend"}
    assert python["urllib3"][0] == "2.6.3"
    assert python["urllib3"][2] is False


def test_parse_package_lock_includes_nested():
    lock = json.dumps(
        {
            "packages": {
                "": {"name": "app"},
                "node_modules/react-router": {"version": "5.3.4"},
                "node_modules/react-router-dom-v5-compat/node_modules/"
                "react-router": {"version": "6.30.3"},
            }
        }
    )
    assert ct.parse_package_lock(lock)["react-router"] == {"5.3.4", "6.30.3"}


def test_parse_yarn_lock_v1_and_berry():
    v1 = (
        'react-router@5.3.4, react-router@5.3.x:\n  version "5.3.4"\n'
        '"@scope/pkg@^1.0.0":\n  version "1.2.0"\n'
    )
    berry = '"react-router@npm:6.30.0":\n  version: 6.30.0\n'
    assert ct.parse_yarn_lock(v1) == {
        "react-router": {"5.3.4"},
        "@scope/pkg": {"1.2.0"},
    }
    assert ct.parse_yarn_lock(berry) == {"react-router": {"6.30.0"}}


def test_found_packages_needles_per_binary():
    go = {
        "usr/bin/oc": (
            {"go.mongodb.org/mongo-driver": "v1.14.0"},
            True,
            set(),
        )
    }
    found = ct.found_packages(
        ["go.mongodb.org/mongo-driver"],
        go,
        {},
        {},
        "",
        {"go.mongodb.org/mongo-driver": ["mongo-driver/mongo/gridfs"]},
    )
    assert found == [
        FoundPackage(
            "go.mongodb.org/mongo-driver",
            "v1.14.0",
            "/usr/bin/oc",
            True,
            False,
        )
    ]


# --- classification (session tickets) -------------------------------------------

GRPC_FIXES = ct.ranges_from_text("Prior to 1.82.2 and 1.83.2")
OCP_CLI = BaseImage(
    ref="registry.redhat.io/openshift4/ose-cli-rhel9@sha256:ff91",
    component="openshift4/ose-cli-rhel9",
    stream="4.20",
)
OCP_OPERATOR = BaseImage(
    ref="registry.redhat.io/openshift4/ose-ansible-rhel9-operator:v4.18.0",
    component="openshift4/ose-ansible-rhel9-operator",
    stream="4.18",
)


def _classify(**kwargs):
    facts = Facts(ticket=kwargs.pop("ticket", _ticket()), **kwargs)
    return ct.classify(facts, JIRA)[0]


def test_classify_fixed_in_build_mtv_6839():
    assert (
        _classify(
            candidates=["google.golang.org/grpc"],
            found=[
                FoundPackage("google.golang.org/grpc", "v1.82.2", "/usr/bin/opa")
            ],
            ranges=GRPC_FIXES,
        )
        == ct.FIXED_IN_BUILD
    )


def test_classify_not_affected_ocp_mtv_6794():
    assert (
        _classify(
            candidates=["github.com/moby/buildkit"],
            found=[
                FoundPackage(
                    "github.com/moby/buildkit", "v0.12.5", "/usr/bin/oc", True
                )
            ],
            ranges=ct.ranges_from_text("Prior to 0.31.2, a custom client"),
            base=OCP_CLI,
            ocp=OcpTracker("OCPBUGS-113907", "Closed", "Not a Bug"),
        )
        == ct.NOT_AFFECTED_OCP
    )


def test_classify_code_absent_mtv_6835():
    assert (
        _classify(
            candidates=["google.golang.org/grpc"],
            found=[
                FoundPackage(
                    "google.golang.org/grpc",
                    "v1.63.2",
                    "/usr/local/bin/ansible-operator",
                    True,
                    False,
                )
            ],
            ranges=GRPC_FIXES,
            base=OCP_OPERATOR,
            ocp=OcpTracker("OCPBUGS-124919", "New", ""),
        )
        == ct.NOT_AFFECTED_CODE_ABSENT
    )


def test_classify_blocked_on_ocp_mtv_6153():
    assert (
        _classify(
            candidates=["pyasn1"],
            found=[
                FoundPackage(
                    "pyasn1", "0.6.3", "/usr/local/lib/python3.12", True
                )
            ],
            ranges=ct.ranges_from_text("Prior to 0.6.4, the decoders"),
            base=OCP_OPERATOR,
            ocp=OcpTracker("OCPBUGS-98760", "New", ""),
        )
        == ct.BLOCKED_ON_OCP
    )


def test_classify_base_bump_needed_when_ocp_fixed():
    assert (
        _classify(
            candidates=["ansible-core"],
            found=[
                FoundPackage("ansible-core", "2.18.1", "/usr/local/lib", True)
            ],
            ranges=ct.ranges_from_text("fixed in version 2.18.14"),
            base=OCP_OPERATOR,
            ocp=OcpTracker("OCPBUGS-1", "Closed", "Done"),
        )
        == ct.BASE_BUMP_NEEDED
    )


def test_classify_affected_when_mtv_builds_vulnerable_version():
    # e.g. MTV-6783 before the code-absent check: mongo-driver 1.17.6 in xcopy
    assert (
        _classify(
            candidates=["go.mongodb.org/mongo-driver"],
            found=[
                FoundPackage(
                    "go.mongodb.org/mongo-driver",
                    "v1.17.6",
                    "/usr/bin/vsphere-xcopy-volume-populator",
                )
            ],
            ranges=ct.ranges_from_text("fixed in version 1.17.10"),
        )
        == ct.AFFECTED
    )


def test_classify_not_affected_version_mtv_7105():
    assert (
        _classify(
            candidates=["react-router"],
            found=[
                FoundPackage("react-router", "5.3.4", "yarn.lock"),
                FoundPackage("react-router", "6.30.0", "yarn.lock"),
            ],
            ranges=ct.ranges_from_text("In versions 7.7.0 through 7.13.1"),
        )
        == ct.NOT_AFFECTED_VERSION
    )


def test_classify_not_built_mtv_6690():
    assert (
        _classify(not_built_reason="bundle ships mtv-must-gather-rhel9")
        == ct.NOT_BUILT
    )


def test_classify_skips_embargoed_mtv_6789():
    assert _classify(ticket=_ticket(embargoed=True)) == ct.SKIPPED


def test_classify_needs_review_when_package_not_found():
    assert (
        _classify(candidates=["openssl"], found=[]) == ct.NEEDS_REVIEW
    )


def test_classify_ocp_not_a_bug_without_fixed_version_mtv_6519():
    assert (
        _classify(
            candidates=["github.com/moby/buildkit"],
            found=[
                FoundPackage(
                    "github.com/moby/buildkit", "v0.12.5", "/usr/bin/oc", True
                )
            ],
            base=OCP_CLI,
            ocp=OcpTracker("OCPBUGS-113907", "Closed", "Not a Bug"),
        )
        == ct.NOT_AFFECTED_OCP
    )


def test_classify_code_absent_without_fixed_version_mtv_6783():
    assert (
        _classify(
            candidates=["go.mongodb.org/mongo-driver"],
            found=[
                FoundPackage(
                    "go.mongodb.org/mongo-driver",
                    "v1.17.6",
                    "/usr/bin/vsphere-xcopy-volume-populator",
                    paths_present=False,
                )
            ],
        )
        == ct.NOT_AFFECTED_CODE_ABSENT
    )


def test_osv_packages_dedupes_and_drops_empty_ranges():
    affected = {
        "package": {"ecosystem": "Go", "name": "google.golang.org/grpc"},
        "ranges": [
            {
                "type": "SEMVER",
                "events": [
                    {"introduced": "0"},
                    {"fixed": "1.82.2"},
                    {"introduced": "1.84.0-dev"},
                    {"fixed": "1.84.0"},
                ],
            }
        ],
    }
    ranges, _ = ct.osv_packages(
        [{"affected": [affected]}, {"affected": [affected]}]
    )
    assert ranges["google.golang.org/grpc"] == [
        ct.VulnRange(introduced=(0,), fixed=(1, 82, 2))
    ]


def test_classify_needs_review_without_ocp_tracker():
    assert (
        _classify(
            candidates=["urllib3"],
            found=[FoundPackage("urllib3", "2.6.3", "/usr/local/lib", True)],
            ranges=ct.ranges_from_text("From 1.26.0 until 2.8.0"),
            base=OCP_OPERATOR,
        )
        == ct.NEEDS_REVIEW
    )


def test_render_comment_has_marker_and_monospace():
    body = ct.render_comment(
        ct.FIXED_IN_BUILD,
        _ticket(),
        "quay.io/x/forklift-api-2-10@sha256:a971",
        ["/usr/local/bin/forklift-api: golang.org/x/text v0.39.0"],
    )
    assert "{{mtv-api-rhel9}}" in body
    assert "* /usr/local/bin/forklift-api: golang.org/x/text v0.39.0" in body
    assert "[cve-triage:FIXED_IN_BUILD]" in body
    assert "release-2.10 z-stream" in body


# --- act_and_report ---------------------------------------------------------------


def _finding(category=ct.FIXED_IN_BUILD):
    return CveFindingDTO(
        ticket=_ticket(), category=category, evidence=["e"], image="img"
    )


def _args(**kwargs):
    values = {"apply": False, "skip_slack": True, "report_dir": ""}
    values.update(kwargs)
    return Namespace(**values)


@pytest.fixture
def triage_config(tmp_path):
    cfg = {
        "report_dir": str(tmp_path),
        "labels": {ct.FIXED_IN_BUILD: "triage:ready-for-assigned"},
    }
    with patch(
        "pipelines.cve_triage.config.get_cve_triage", return_value=cfg
    ), patch("pipelines.cve_triage.config.get_jira_url", return_value=JIRA):
        yield tmp_path


def test_act_and_report_dry_run_writes_report_without_jira(triage_config):
    data = CveFindingsDTO(
        findings=[_finding(), _finding(ct.NEEDS_REVIEW)]
    )
    with patch("pipelines.cve_triage.Jira") as m_jira:
        result = run_task(pipeline.act_and_report, data, _args())
    m_jira.assert_not_called()
    assert result.dry_run is True
    assert result.counts == {ct.FIXED_IN_BUILD: 1, ct.NEEDS_REVIEW: 1}
    report = open(result.report_path).read()
    assert "## FIXED_IN_BUILD (1)" in report
    assert os.path.exists(result.report_path.removesuffix(".md") + ".json")


def test_act_and_report_dry_run_keeps_comment_bodies_out_of_logs(
    triage_config, caplog
):
    finding = _finding()
    finding.evidence = ["/usr/bin/oc: secret-evidence v1"]
    with caplog.at_level("INFO"):
        run_task(
            pipeline.act_and_report,
            CveFindingsDTO(findings=[finding]),
            _args(),
        )
    assert "DRY RUN: would comment on MTV-1" in caplog.text
    assert "secret-evidence" not in caplog.text


def test_slack_summary_skipped_without_private_channel(triage_config):
    with patch("pipelines.cve_triage.Slack") as m_slack:
        pipeline._send_slack([_finding()], {ct.FIXED_IN_BUILD: 1}, "r.md")
    m_slack.assert_not_called()


def test_slack_summary_has_counts_and_links_only(triage_config):
    finding = _finding(ct.AFFECTED)
    cfg = {"slack_channel": "C0PRIVATE"}
    with (
        patch("pipelines.cve_triage.config.get_cve_triage", return_value=cfg),
        patch("pipelines.cve_triage.Slack") as m_slack,
    ):
        pipeline._send_slack([finding], {ct.AFFECTED: 1}, "r.md")
    blocks = m_slack.return_value.send_block.call_args.args[0]
    text = json.dumps(blocks)
    assert "MTV-1" in text and "C0PRIVATE" not in text
    assert "mtv-api-rhel9" not in text and "release-2.10" not in text


def test_act_and_report_apply_comments_and_labels(triage_config):
    jira = MagicMock()
    jira.get_comments.return_value = []
    with patch("pipelines.cve_triage.Jira", return_value=jira):
        result = run_task(
            pipeline.act_and_report,
            CveFindingsDTO(findings=[_finding()]),
            _args(apply=True),
        )
    assert result.applied == 1
    jira.add_comment.assert_called_once()
    jira.add_labels.assert_called_once_with(
        "MTV-1", ["triage:ready-for-assigned"]
    )


def test_act_and_report_apply_labels_but_does_not_recomment(triage_config):
    # A previous run commented but failed to label: label now, no 2nd comment.
    jira = MagicMock()
    jira.get_comments.return_value = ["old _[cve-triage:FIXED_IN_BUILD]_"]
    with patch("pipelines.cve_triage.Jira", return_value=jira):
        result = run_task(
            pipeline.act_and_report,
            CveFindingsDTO(findings=[_finding()]),
            _args(apply=True),
        )
    assert result.applied == 0
    jira.add_comment.assert_not_called()
    jira.add_labels.assert_called_once_with(
        "MTV-1", ["triage:ready-for-assigned"]
    )


# --- failure handling (PR #61 review) -----------------------------------------

API_BUNDLE = {
    "API_IMAGE": "registry.redhat.io/migration-toolkit-virtualization/"
    "mtv-api-rhel9@sha256:abc"
}
OCP_CONTAINERFILE = "FROM registry.redhat.io/openshift4/ose-cli-rhel9:v4.20\n"


def test_fetch_raw_returns_none_on_network_error():
    import requests

    with patch(
        "pipelines.cve_triage.requests.get",
        side_effect=requests.ConnectionError("down"),
    ):
        assert pipeline._fetch_raw("kubev2v/forklift", "main", "x") is None


def test_scan_errors_when_containerfile_missing():
    with (
        patch("pipelines.cve_triage._fetch_raw", return_value=None),
        patch("pipelines.cve_triage.Skopeo") as m_skopeo,
    ):
        result = pipeline._scan_component(
            "release-2.10", "mtv-api-rhel9", API_BUNDLE, set()
        )
    assert "Could not fetch" in result["error"]
    m_skopeo.return_value.copy.assert_not_called()


def test_scan_errors_when_base_inspect_fails():
    # Without base layers everything would look MTV-built -> wrong AFFECTED.
    skopeo = MagicMock()
    skopeo.inspect.side_effect = RuntimeError("unauthorized")
    with (
        patch(
            "pipelines.cve_triage._fetch_raw", return_value=OCP_CONTAINERFILE
        ),
        patch("pipelines.cve_triage.Skopeo", return_value=skopeo),
    ):
        result = pipeline._scan_component(
            "release-2.10", "mtv-api-rhel9", API_BUNDLE, set()
        )
    assert "Could not inspect base image" in result["error"]
    skopeo.copy.assert_not_called()
    category, _ = ct.classify(
        Facts(ticket=_ticket(), error=result["error"]), JIRA
    )
    assert category == ct.NEEDS_REVIEW


def test_scan_converts_archive_errors_into_error_result():
    skopeo = MagicMock()
    skopeo.inspect.return_value = {
        "Layers": ["sha256:base"],
        "Labels": {"version": "v4.20.0"},
    }
    with (
        patch(
            "pipelines.cve_triage._fetch_raw", return_value=OCP_CONTAINERFILE
        ),
        patch("pipelines.cve_triage.Skopeo", return_value=skopeo),
        patch(
            "pipelines.cve_triage.scan_image_dir",
            side_effect=tarfile.ReadError("bad blob"),
        ),
    ):
        result = pipeline._scan_component(
            "release-2.10", "mtv-api-rhel9", API_BUNDLE, set()
        )
    assert "bad blob" in result["error"]


def test_lookup_ocp_failure_returns_none_and_is_cached():
    import requests

    jira = MagicMock()
    jira.search.side_effect = requests.ConnectionError("down")
    cache = {}
    assert pipeline._lookup_ocp(jira, cache, "CVE-1", OCP_CLI) == (None, None)
    assert pipeline._lookup_ocp(jira, cache, "CVE-1", OCP_CLI) == (None, None)
    jira.search.assert_called_once()


def _ocp_issue(key, stream):
    return {
        "key": key,
        "fields": {
            "summary": f"CVE-1 openshift4/ose-cli-rhel9: x [openshift-{stream}]",
            "status": {"name": "New"},
        },
    }


def test_lookup_ocp_returns_tracker_and_streams():
    jira = MagicMock()
    jira.search.return_value = [
        _ocp_issue("OCPBUGS-3", "4.21"),
        _ocp_issue("OCPBUGS-1", "4.16.z"),
        _ocp_issue("OCPBUGS-2", "4.20"),
    ]
    tracker, streams = pipeline._lookup_ocp(jira, {}, "CVE-1", OCP_CLI)
    assert tracker.key == "OCPBUGS-2"
    assert streams == ["4.16.z", "4.20", "4.21"]


@pytest.mark.parametrize(
    "streams, expected",
    [
        (None, "The OCPBUGS lookup failed."),
        ([], "returned no trackers"),
        (["4.16.z", "4.19"], "returned trackers for 4.16.z, 4.19 only."),
    ],
)
def test_classify_explains_missing_ocp_tracker(streams, expected):
    # MTV-6175 on 2026-10-23: a transient search miss for OCPBUGS-98894.
    facts = Facts(
        ticket=_ticket(),
        candidates=["pyasn1"],
        found=[FoundPackage("pyasn1", "0.6.1", "/usr/local/lib", True)],
        ranges=ct.ranges_from_text("Prior to 0.6.4"),
        base=OCP_OPERATOR,
        ocp_streams=streams,
    )
    category, evidence = ct.classify(facts, JIRA)
    assert category == ct.NEEDS_REVIEW
    assert expected in evidence[-1]


def test_title_aliases_cover_ansible_galaxy_and_pip():
    from config import config

    aliases = config.get_cve_triage()["package_aliases"]
    galaxy = (
        "CVE-2026-11332 ns/mtv-rhel9-operator: argument injection in "
        "ansible-galaxy role install leads to arbitrary code execution "
        "[mtv-2.12]"
    )
    pip = (
        "CVE-2026-8643 ns/mtv-rhel9-operator: Path traversal via malicious "
        "entry point name in pip wheel installation allows arbitrary file "
        "overwrite [mtv-2.11]"
    )
    assert ct.candidates_from_title(galaxy, aliases) == ["ansible-core"]
    assert ct.candidates_from_title(pip, aliases) == ["pip"]


def _osv_response(status, payload=None):
    resp = MagicMock(status_code=status)
    resp.json.return_value = payload or {}
    return resp


def test_osv_missing_record_returns_none():
    from wrappers import osv

    with patch("wrappers.osv.requests.get", return_value=_osv_response(404)):
        assert osv.get_vuln("CVE-2026-1") is None


@pytest.mark.parametrize("status", [429, 500, 503])
def test_osv_http_errors_raise(status):
    from wrappers import osv

    with patch(
        "wrappers.osv.requests.get", return_value=_osv_response(status)
    ):
        with pytest.raises(osv.OsvLookupError):
            osv.get_vuln("CVE-2026-1")


def test_osv_network_error_raises():
    import requests
    from wrappers import osv

    with patch(
        "wrappers.osv.requests.get", side_effect=requests.Timeout("slow")
    ):
        with pytest.raises(osv.OsvLookupError):
            osv.get_vuln("CVE-2026-1")


def test_analyze_osv_outage_is_needs_review_not_fallback():
    # Without the guard, the title alias + flaw-text range would classify this
    # ticket from weaker evidence and it would get a permanent label.
    from wrappers.osv import OsvLookupError

    ticket = _ticket(description="Prior to 0.39.0")
    with (
        patch("pipelines.cve_triage.Skopeo"),
        patch("pipelines.cve_triage.Jira"),
        patch("pipelines.cve_triage._fetch_raw", return_value=None),
        patch(
            "pipelines.cve_triage.osv.get_vuln",
            side_effect=OsvLookupError("OSV returned 503"),
        ),
    ):
        result = run_task(
            pipeline.analyze,
            pipeline.CveTicketsDTO(tickets=[ticket]),
            _args(),
        )
    [finding] = result.findings
    assert finding.category == ct.NEEDS_REVIEW
    assert "OSV returned 503" in finding.evidence[0]
    assert "retry" in finding.evidence[0]


def test_skopeo_auth_prod_skips_stage_login():
    from wrappers.skopeo import Skopeo

    with patch("wrappers.skopeo.subprocess.run") as m_run:
        m_run.return_value = MagicMock(returncode=0, stdout=b"")
        Skopeo().auth_prod()
    commands = [c.args[0][-1] for c in m_run.call_args_list]
    assert commands == [
        "skopeo login registry.redhat.io "
        "-u $REGISTRY_PROD_USER -p $REGISTRY_PROD_TOKEN"
    ]
