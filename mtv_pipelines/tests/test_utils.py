import os
from unittest.mock import MagicMock

import pytest
from models.dto import JenkinsJobDTO
from semver import Version

import utils


@pytest.fixture(autouse=True)
def clear_temp_dirs():
    utils._temp_dirs.clear()
    yield
    utils.cleanup_temp_dirs()


class TestCreateTempDir:
    def test_creates_directory_with_suffix(self):
        path = utils.create_temp_dir("bundle")
        assert os.path.isdir(path)
        assert path.endswith("_bundle")

    def test_creates_directory_without_suffix(self):
        path = utils.create_temp_dir()
        assert os.path.isdir(path)

    def test_cleanup_removes_tracked_directories(self):
        path = utils.create_temp_dir("cleanup")
        utils.cleanup_temp_dirs()
        assert not os.path.isdir(path)
        assert utils._temp_dirs == []


class TestReplaceForQuay:
    def test_non_redhat_image_is_unchanged(self):
        image = "docker.io/library/nginx:latest"
        assert utils.replace_for_quay(image, Version(2, 11, 0)) == image

    def test_release_namespace_with_tag(self):
        image = (
            "registry.redhat.io/migration-toolkit-virtualization/"
            "mtv-api-rhel9:2.11.0"
        )
        result = utils.replace_for_quay(image, Version(2, 11, 0))
        assert result == (
            "quay.io/redhat-user-workloads/rh-mtv-1-tenant/"
            "forklift-operator-2-11/forklift-api-2-11:2.11.0"
        )

    def test_release_namespace_with_digest(self):
        image = (
            "registry.redhat.io/migration-toolkit-virtualization/"
            "mtv-api-rhel9@sha256:abc123"
        )
        result = utils.replace_for_quay(image, Version(2, 11, 0))
        assert result == (
            "quay.io/redhat-user-workloads/rh-mtv-1-tenant/"
            "forklift-operator-2-11/forklift-api-2-11@sha256:abc123"
        )

    def test_dev_preview_namespace(self):
        image = "registry.redhat.io/mtv-candidate/mtv-api-rhel9:2.12.0"
        result = utils.replace_for_quay(image, Version(2, 12, 0))
        assert result == (
            "quay.io/redhat-user-workloads/rh-mtv-1-tenant/"
            "forklift-operator-dev-preview/forklift-api-dev-preview:2.12.0"
        )

    def test_rhel10_component_uses_btrfs_tenant(self):
        image = (
            "registry.redhat.io/migration-toolkit-virtualization/"
            "mtv-virt-v2v-rhel10:2.11.0"
        )
        result = utils.replace_for_quay(image, Version(2, 11, 0))
        assert result.startswith(
            "quay.io/redhat-user-workloads/rh-mtv-btrfs-tenant/"
            "forklift-operator-int-2-11/virt-v2v-int-2-11:"
        )


class TestParseVersion:
    def test_patch_gt_zero_sets_previous_iib(self):
        fbc = MagicMock()
        fbc.for_bundle.ocps = ["v4.21"]

        utils.parse_version(fbc, Version(2, 11, 2))

        assert fbc.previous_iib_version == Version(2, 11, 1)
        assert fbc.previous_iib.version == Version(2, 11, 1)
        assert fbc.previous_iib.url == (
            "registry.redhat.io/redhat/redhat-operator-index:v4.21"
        )

    def test_patch_zero_does_not_set_previous_iib(self):
        class FBC:
            def __init__(self):
                self.for_bundle = MagicMock(ocps=["v4.21"])
                self.previous_iib_version = None
                self.previous_iib = None

        fbc = FBC()
        utils.parse_version(fbc, Version(2, 12, 0))

        assert fbc.previous_iib_version is None
        assert fbc.previous_iib is None


class TestExtractJiraKeys:
    def test_resolves_none_returns_empty(self):
        assert utils.extract_jira_keys("Resolves: none") == []

    def test_chore_without_resolves_returns_empty(self):
        assert utils.extract_jira_keys("chore(deps): bump something") == []

    def test_extracts_allowed_project_key(self):
        assert utils.extract_jira_keys("MTV-1234 fix migration bug") == [
            "MTV-1234"
        ]

    def test_resolves_section_takes_priority_over_chore_prefix(self):
        text = "chore(release): update version\nResolves: MTV-9999"
        assert utils.extract_jira_keys(text) == ["MTV-9999"]

    def test_only_searches_before_pipe_without_resolves(self):
        text = "MTV-1111 initial change | MTV-2222 trailer"
        assert utils.extract_jira_keys(text) == ["MTV-1111"]

    def test_filters_disallowed_project_keys(self):
        assert utils.extract_jira_keys("FOO-1234 unrelated change") == []

    def test_deduplicates_keys(self):
        text = "MTV-1234 first mention and MTV-1234 again"
        assert utils.extract_jira_keys(text) == ["MTV-1234"]


class TestForkliftBranchFromJenkinsJob:
    def test_release_branch(self):
        job = JenkinsJobDTO(
            iib_version="2.11.0-38",
            job_name="test",
            build_number=1,
            ocp_version="v4.21",
            job_url="http://jenkins/job/test/1",
        )
        assert utils.forklift_branch_from_jenkins_job(job) == "release-2.11"

    def test_main_branch(self):
        job = JenkinsJobDTO(
            iib_version="2.12.0-1",
            job_name="test",
            build_number=1,
            ocp_version="v4.22",
            job_url="http://jenkins/job/test/1",
        )
        assert utils.forklift_branch_from_jenkins_job(job) == "main"

    def test_unknown_version_raises(self):
        job = JenkinsJobDTO(
            iib_version="1.0.0-1",
            job_name="test",
            build_number=1,
            ocp_version="v4.18",
            job_url="http://jenkins/job/test/1",
        )
        with pytest.raises(ValueError, match="No forklift branch found"):
            utils.forklift_branch_from_jenkins_job(job)
