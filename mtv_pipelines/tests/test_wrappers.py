"""Tests for wrappers/*.py — all external I/O is mocked."""

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import git as gitpython
import pytest

from wrappers.git import Git
from wrappers.gh_cli import GHCLI
from wrappers.jenkins import JenkinsManager
from wrappers.jenkins_analyzer import JenkinsAnalyzer
from wrappers.skopeo import Skopeo
from wrappers.slack import Slack, SlackBuilder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    """Run an async coroutine in tests without pytest-asyncio."""
    return asyncio.run(coro)


def _make_subprocess_result(stdout: bytes = b"", returncode: int = 0):
    r = MagicMock()
    r.stdout = stdout
    r.returncode = returncode
    if returncode != 0:
        r.check_returncode.side_effect = subprocess.CalledProcessError(
            returncode, "cmd", stderr=b"some error"
        )
    else:
        r.check_returncode.return_value = None
    r.stderr = b"some error"
    return r


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


class TestGit:
    @pytest.fixture
    def g(self):
        """Git instance with a pre-attached mock repo."""
        instance = Git.__new__(Git)
        instance.repo_path = "/tmp/test_repo"
        instance.repo = MagicMock()
        return instance

    # -- clone ---------------------------------------------------------------

    def test_clone_calls_clone_from(self):
        g = Git("/tmp/repo")
        mock_repo = MagicMock()
        # Patch asyncio.to_thread so no real OS thread is spawned; also patch
        # clone_from so we can verify it is the callable passed to to_thread.
        with (
            patch("wrappers.git.git.Repo.clone_from", return_value=mock_repo) as m_clone,
            patch(
                "wrappers.git.asyncio.to_thread",
                new_callable=AsyncMock,
                return_value=mock_repo,
            ) as m_thread,
        ):
            run(g.clone("https://github.com/org/repo"))
        m_thread.assert_called_once_with(
            m_clone, "https://github.com/org/repo", "/tmp/repo"
        )
        assert g.repo is mock_repo

    # -- pull ----------------------------------------------------------------

    def test_pull_success(self, g):
        mock_remote = MagicMock()
        g.repo.remote.return_value = mock_remote

        with patch("wrappers.git.asyncio.to_thread", new_callable=AsyncMock) as m:
            run(g.pull("main"))
        g.repo.remote.assert_called_once_with(name="origin")
        m.assert_called_once_with(mock_remote.pull, "main")

    def test_pull_raises_on_missing_remote(self, g):
        g.repo.remote.side_effect = ValueError("no remote")
        with pytest.raises(RuntimeError, match="Remote origin not found"):
            run(g.pull())

    def test_pull_raises_on_git_error(self, g):
        mock_remote = MagicMock()
        g.repo.remote.return_value = mock_remote
        # Use AsyncMock so the exception is raised at await-time, matching the
        # real semantics of awaiting asyncio.to_thread.
        with patch(
            "wrappers.git.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=gitpython.GitCommandError("pull", "error"),
        ):
            with pytest.raises(RuntimeError, match="Error pulling changes"):
                run(g.pull())

    # -- push ----------------------------------------------------------------

    def test_push_calls_git_push(self, g):
        g.push("feature", push_options=["merge_request.create"])
        g.repo.git.push.assert_called_once_with(
            "origin",
            "feature:feature",
            "-o",
            "merge_request.create",
        )

    def test_push_without_options(self, g):
        g.push("main")
        g.repo.git.push.assert_called_once_with("origin", "main:main")

    def test_push_raises_on_git_error(self, g):
        g.repo.git.push.side_effect = gitpython.GitCommandError("push", "denied")
        with pytest.raises(RuntimeError, match="Error pushing changes"):
            g.push("main")

    # -- log -----------------------------------------------------------------

    def test_log_returns_commit_list(self, g):
        commit = MagicMock()
        commit.hexsha = "abc123"
        commit.author.name = "Alice"
        commit.committed_datetime = datetime(2024, 1, 1, tzinfo=timezone.utc)
        commit.message = "fix bug\n"
        g.repo.iter_commits.return_value = [commit]

        logs = g.log(max_count=5)

        assert len(logs) == 1
        assert logs[0]["sha"] == "abc123"
        assert logs[0]["author"] == "Alice"
        assert logs[0]["message"] == "fix bug"
        g.repo.iter_commits.assert_called_once_with(max_count=5)

    # -- branches / remote_branches ------------------------------------------

    def test_branches_returns_names(self, g):
        g.repo.heads = [MagicMock(name="main"), MagicMock(name="dev")]
        g.repo.heads[0].name = "main"
        g.repo.heads[1].name = "dev"
        assert g.branches() == ["main", "dev"]

    def test_remote_branches_returns_ref_names(self, g):
        ref = MagicMock()
        ref.name = "origin/main"
        remote = MagicMock()
        remote.refs = [ref]
        g.repo.remotes = [remote]
        assert g.remote_branches() == ["origin/main"]

    # -- checkout ------------------------------------------------------------

    def test_checkout_existing_branch(self, g):
        g.repo.head.is_detached = False
        g.repo.active_branch.name = "main"
        g.checkout("main")
        g.repo.git.checkout.assert_called_once_with("main")

    def test_checkout_create_new_branch(self, g):
        g.repo.head.is_detached = False
        g.repo.active_branch.name = "new-branch"
        g.checkout("new-branch", create=True)
        g.repo.git.checkout.assert_called_once_with("-b", "new-branch")

    # -- has_changes / add_files / commit ------------------------------------

    def test_has_changes_delegates_to_is_dirty(self, g):
        g.repo.is_dirty.return_value = True
        g.repo.git.execute.return_value = ""
        assert g.has_changes() is True
        g.repo.is_dirty.assert_called_once_with(untracked_files=True)

    def test_add_files(self, g):
        g.add_files(["file.py", "other.py"])
        g.repo.index.add.assert_called_once_with(["file.py", "other.py"])

    def test_commit(self, g):
        g.commit("MTV-1234 fix bug")
        g.repo.git.commit.assert_called_once_with("-s", "-m", "MTV-1234 fix bug")

    # -- _ensure_repo --------------------------------------------------------

    def test_ensure_repo_raises_when_none(self):
        g = Git.__new__(Git)
        g.repo_path = "/tmp/x"
        g.repo = None
        with pytest.raises(RuntimeError, match="Repository not initialized"):
            g._ensure_repo()


# ---------------------------------------------------------------------------
# GHCLI
# ---------------------------------------------------------------------------


class TestGHCLI:
    @pytest.fixture
    def cli(self):
        # Use a plain string — GHCLI stores cwd only to pass to subprocess.run
        # (which is always mocked), so no real directory needs to exist.
        return GHCLI("/fake/cwd")

    def test_list_pr_returns_parsed_json(self, cli):
        payload = [{"url": "https://github.com/org/repo/pull/1"}]
        result = _make_subprocess_result(json.dumps(payload).encode())
        with patch("wrappers.gh_cli.subprocess.run", return_value=result):
            out = cli.list_pr("release-2.11")
        assert out == payload

    def test_create_pr_returns_url(self, cli):
        output = b"https://github.com/kubev2v/forklift/pull/42\n"
        result = _make_subprocess_result(output)
        with patch("wrappers.gh_cli.subprocess.run", return_value=result):
            url = cli.create_pr("v2.11.0", target_branch="main")
        assert url == "https://github.com/kubev2v/forklift/pull/42"

    def test_create_pr_raises_when_url_missing(self, cli):
        result = _make_subprocess_result(b"no url here")
        with patch("wrappers.gh_cli.subprocess.run", return_value=result):
            with pytest.raises(RuntimeError, match="Couldn't extract PR URL"):
                cli.create_pr("v2.11.0")

    def test_list_pr_checks_returns_parsed_json(self, cli):
        payload = [{"name": "lint", "state": "SUCCESS"}]
        result = _make_subprocess_result(json.dumps(payload).encode())
        with patch("wrappers.gh_cli.subprocess.run", return_value=result):
            out = cli.list_pr_checks("https://github.com/org/repo/pull/1")
        assert out == payload

    def test_comment_on_pr_succeeds(self, cli):
        result = _make_subprocess_result(b"")
        with patch("wrappers.gh_cli.subprocess.run", return_value=result) as m:
            cli.comment_on_pr("https://github.com/org/repo/pull/1", "/retest")
        m.assert_called_once()

    def test_exec_raises_runtime_error_on_failure(self, cli):
        result = _make_subprocess_result(b"", returncode=1)
        with patch("wrappers.gh_cli.subprocess.run", return_value=result):
            with pytest.raises(RuntimeError):
                cli.list_pr("some-branch")


# ---------------------------------------------------------------------------
# JenkinsManager
# ---------------------------------------------------------------------------


class TestJenkinsManager:
    @pytest.fixture
    def mgr(self):
        with (
            patch("wrappers.jenkins.JenkinsAuth") as mock_auth,
            patch("wrappers.jenkins.jenkins.Jenkins") as mock_jenkins,
            patch(
                "wrappers.jenkins.config.get_root_cert_path",
                return_value="/certs/root.pem",
            ),
        ):
            mock_auth.return_value = MagicMock(user="u", token="t")
            instance = JenkinsManager("http://jenkins.example.com")
            instance.server = mock_jenkins.return_value
            yield instance

    # -- trigger_job ---------------------------------------------------------

    def test_trigger_job_returns_queue_id(self, mgr):
        mgr.server.build_job.return_value = 99
        assert mgr.trigger_job("my-job") == 99
        mgr.server.build_job.assert_called_once_with("my-job", parameters=None)

    def test_trigger_job_returns_zero_on_not_found(self, mgr):
        import jenkins as jenkins_lib

        mgr.server.build_job.side_effect = jenkins_lib.NotFoundException()
        assert mgr.trigger_job("missing-job") == 0

    def test_trigger_job_with_params(self, mgr):
        mgr.server.build_job.return_value = 7
        mgr.trigger_job("job", params={"KEY": "val"})
        mgr.server.build_job.assert_called_once_with("job", parameters={"KEY": "val"})

    # -- wait_for_build_to_start ---------------------------------------------

    def test_wait_for_build_to_start_returns_build_number(self, mgr):
        mgr.server.get_queue_item.return_value = {"executable": {"number": 42}}
        with (
            patch(
                "wrappers.jenkins.config.get_jenkins_wait_refresh_seconds",
                return_value=0,
            ),
            # Explicitly mock asyncio.sleep so no real sleep can happen even if
            # the mock conditions change and the loop iterates more than once.
            patch("wrappers.jenkins.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = run(mgr.wait_for_build_to_start(5))
        assert result == 42

    # -- get_job_info --------------------------------------------------------

    def test_get_job_info_returns_info_dict(self, mgr):
        mgr.server.get_build_info.return_value = {"building": False, "result": "SUCCESS"}
        info = run(mgr.get_job_info("my-job", 3))
        assert info["result"] == "SUCCESS"
        mgr.server.get_build_info.assert_called_once_with("my-job", 3)

    # -- get_test_release_gate_args ------------------------------------------

    def test_release_gate_args_includes_cluster(self, mgr):
        with patch(
            "wrappers.jenkins.config.get_cluster_mappings",
            return_value={"4.21": "cluster-a"},
        ):
            args = mgr.get_test_release_gate_args("2.11.0", "4.21", "iib-123")
        assert args["CLUSTER_NAME"] == "cluster-a"
        assert args["MATRIX_TYPE"] == "RELEASE"
        assert args["IIB_NO"] == "iib-123"
        assert args["MTV_VERSION"] == "2.11.0"
        assert args["OCP_VERSION"] == "4.21"

    def test_release_gate_args_raises_on_unknown_ocp(self, mgr):
        with patch(
            "wrappers.jenkins.config.get_cluster_mappings",
            return_value={"4.21": "cluster-a"},
        ):
            with pytest.raises(ValueError, match="not in cluster mappings"):
                mgr.get_test_release_gate_args("2.11.0", "9.99", "iib-x")

    def test_release_gate_args_returns_empty_when_cluster_none(self, mgr):
        with patch(
            "wrappers.jenkins.config.get_cluster_mappings",
            return_value={"4.21": "none"},
        ):
            args = mgr.get_test_release_gate_args("2.11.0", "4.21", "iib-x")
        assert args == {}

    # -- get_test_release_non_gate_args --------------------------------------

    def test_non_gate_args_sets_tier1_matrix(self, mgr):
        with patch(
            "wrappers.jenkins.config.get_cluster_mappings",
            return_value={"4.21": "cluster-b"},
        ):
            args = mgr.get_test_release_non_gate_args("2.11.0", "4.21", "iib-456")
        assert args["MATRIX_TYPE"] == "TIER1"

    # -- trigger_release_gate (async) ----------------------------------------

    def test_trigger_release_gate_returns_job_info(self, mgr):
        with (
            patch(
                "wrappers.jenkins.config.get_cluster_mappings",
                return_value={"4.21": "cluster-a"},
            ),
            patch.object(mgr, "run_job", new_callable=AsyncMock, return_value=10),
        ):
            result = run(mgr.trigger_release_gate("2.11.0", "v4.21", "iib-1"))
        assert result["job_name"] == "mtv-2.11-ocp-4.21-test-release-gate"
        assert result["job_number"] == 10

    def test_trigger_release_gate_returns_empty_when_no_job(self, mgr):
        with (
            patch(
                "wrappers.jenkins.config.get_cluster_mappings",
                return_value={"4.21": "none"},
            ),
        ):
            result = run(mgr.trigger_release_gate("2.11.0", "v4.21", "iib-1"))
        assert result == {}


# ---------------------------------------------------------------------------
# JenkinsAnalyzer
# ---------------------------------------------------------------------------


class TestJenkinsAnalyzer:
    @pytest.fixture
    def analyzer(self):
        return JenkinsAnalyzer()

    @pytest.fixture
    def success_job(self):
        from models.dto import JenkinsJobDTO, JenkinsJobResultDTO

        return JenkinsJobResultDTO(
            job=JenkinsJobDTO(
                iib_version="2.11.0-38",
                job_name="mtv-ci",
                build_number=5,
                ocp_version="v4.21",
                job_url="http://jenkins/job/mtv-ci/5",
            ),
            result="SUCCESS",
            url="http://jenkins/job/mtv-ci/5",
        )

    @pytest.fixture
    def failed_job(self):
        from models.dto import JenkinsJobDTO, JenkinsJobResultDTO

        return JenkinsJobResultDTO(
            job=JenkinsJobDTO(
                iib_version="2.11.0-38",
                job_name="mtv-ci",
                build_number=5,
                ocp_version="v4.21",
                job_url="http://jenkins/job/mtv-ci/5",
            ),
            result="FAILURE",
            url="http://jenkins/job/mtv-ci/5",
        )

    def test_analyze_non_failure_returns_empty_dto(self, analyzer, success_job):
        result = analyzer.analyze_job(success_job)
        assert result.summary == ""
        assert result.child_jobs == []

    def test_analyze_failure_calls_requests_post(self, analyzer, failed_job):
        api_response = {
            "summary": "Test failed due to X",
            "result_url": "http://results/1",
            "child_job_analyses": [],
        }
        mock_resp = MagicMock()
        mock_resp.content = json.dumps(api_response).encode()
        mock_resp.raise_for_status.return_value = None

        with (
            patch("wrappers.jenkins_analyzer.requests.post", return_value=mock_resp),
            patch(
                "wrappers.jenkins_analyzer.RootcozAuth",
                return_value=MagicMock(bearer_header="Bearer token"),
            ),
            patch(
                "wrappers.jenkins_analyzer.config.get_jenkins_analyzer_url",
                return_value="http://analyzer",
            ),
            patch(
                "wrappers.jenkins_analyzer.forklift_branch_from_jenkins_job",
                return_value="release-2.11",
            ),
        ):
            result = analyzer.analyze_job(failed_job)

        assert result.summary == "Test failed due to X"
        assert result.html_report_url == "http://results/1"

    def test_process_data_builds_child_jobs(self, analyzer, failed_job):
        data = {
            "summary": "Build exploded",
            "result_url": "http://report/2",
            "child_job_analyses": [
                {
                    "job_name": "child-job",
                    "build_number": 3,
                    "jenkins_url": "http://jenkins/child/3",
                    "summary": "child failed",
                }
            ],
        }
        result = analyzer._process_data(data, failed_job)
        assert len(result.child_jobs) == 1
        assert result.child_jobs[0].job_name == "child-job"
        assert result.child_jobs[0].build_number == 3
        assert result.child_jobs[0].summary == "child failed"

    def test_process_data_fallback_summary(self, analyzer, failed_job):
        data = {"child_job_analyses": []}
        result = analyzer._process_data(data, failed_job)
        assert result.summary == "No summary available from the analyzer."

    def test_process_data_fallback_report_url(self, analyzer, failed_job):
        data = {"summary": "ok", "child_job_analyses": []}
        result = analyzer._process_data(data, failed_job)
        assert result.html_report_url == failed_job.url

    def test_process_data_skips_non_dict_children(self, analyzer, failed_job):
        data = {
            "summary": "ok",
            "child_job_analyses": ["not-a-dict", None],
        }
        result = analyzer._process_data(data, failed_job)
        assert result.child_jobs == []


# ---------------------------------------------------------------------------
# Skopeo
# ---------------------------------------------------------------------------


class TestSkopeo:
    @pytest.fixture
    def skopeo(self):
        return Skopeo()

    def test_prepare_url_adds_protocol(self, skopeo):
        assert skopeo.__prepare_url__("registry.redhat.io/img:tag") == (
            "docker://registry.redhat.io/img:tag"
        )

    def test_prepare_url_keeps_existing_protocol(self, skopeo):
        url = "docker://registry.redhat.io/img:tag"
        assert skopeo.__prepare_url__(url) == url

    def test_inspect_returns_dict(self, skopeo):
        payload = {"Name": "registry.redhat.io/img", "Digest": "sha256:abc"}
        result = _make_subprocess_result(json.dumps(payload).encode())
        with patch("wrappers.skopeo.subprocess.run", return_value=result):
            out = skopeo.inspect("registry.redhat.io/img:latest")
        assert out == payload

    def test_inspect_builds_correct_command(self, skopeo):
        result = _make_subprocess_result(b"{}")
        with patch("wrappers.skopeo.subprocess.run", return_value=result) as m:
            skopeo.inspect("registry.redhat.io/img:latest")
        cmd = m.call_args[0][0]
        assert "inspect" in cmd
        assert "--no-tags" in cmd
        assert "docker://registry.redhat.io/img:latest" in cmd

    def test_copy_builds_correct_command(self, skopeo):
        result = _make_subprocess_result(b"")
        with patch("wrappers.skopeo.subprocess.run", return_value=result) as m:
            skopeo.copy("registry.redhat.io/img:latest", "/tmp/img")
        cmd = m.call_args[0][0]
        assert "copy" in cmd
        assert "docker://registry.redhat.io/img:latest" in cmd
        assert "dir:///tmp/img" in cmd

    def test_exec_raises_on_subprocess_error(self, skopeo):
        result = _make_subprocess_result(b"", returncode=1)
        with patch("wrappers.skopeo.subprocess.run", return_value=result):
            with pytest.raises(RuntimeError):
                skopeo.inspect("bad-image")


# ---------------------------------------------------------------------------
# SlackBuilder (pure builder — no mocks needed)
# ---------------------------------------------------------------------------


class TestSlackBuilder:
    def test_header_block(self):
        blocks = SlackBuilder().header("Hello").build()
        assert len(blocks) == 1
        assert blocks[0]["type"] == "header"
        assert blocks[0]["text"]["text"] == "Hello"

    def test_section_block(self):
        blocks = SlackBuilder().section("body text").build()
        assert blocks[0]["type"] == "section"
        assert blocks[0]["text"]["text"] == "body text"

    def test_section_with_fields(self):
        blocks = SlackBuilder().section("text", fields=["f1", "f2"]).build()
        assert len(blocks[0]["fields"]) == 2

    def test_section_truncates_fields_to_ten(self):
        blocks = SlackBuilder().section("t", fields=[str(i) for i in range(15)]).build()
        assert len(blocks[0]["fields"]) == 10

    def test_divider_block(self):
        blocks = SlackBuilder().divider().build()
        assert blocks[0] == {"type": "divider"}

    def test_context_block(self):
        blocks = SlackBuilder().context(["note1", "note2"]).build()
        assert blocks[0]["type"] == "context"
        assert len(blocks[0]["elements"]) == 2

    def test_chaining_accumulates_blocks(self):
        sb = SlackBuilder()
        sb.header("H").divider().section("S")
        assert len(sb.build()) == 3

    def test_to_json_is_valid_json(self):
        sb = SlackBuilder().header("test").divider()
        parsed = json.loads(sb.to_json())
        assert len(parsed) == 2

    def test_rich_text_text_no_style(self):
        el = SlackBuilder.RichText.text("hello")
        assert el == {"type": "text", "text": "hello"}

    def test_rich_text_text_with_bold(self):
        el = SlackBuilder.RichText.text("hello", bold=True)
        assert el["style"]["bold"] is True

    def test_rich_text_link_with_text(self):
        el = SlackBuilder.RichText.link("http://example.com", "click me")
        assert el["url"] == "http://example.com"
        assert el["text"] == "click me"

    def test_rich_text_link_without_text(self):
        el = SlackBuilder.RichText.link("http://example.com")
        assert "text" not in el

    def test_table_row_structure(self):
        RT = SlackBuilder.RichText
        row = SlackBuilder.Table.row([[RT.text("a")], [RT.text("b")]])
        assert len(row) == 2
        assert row[0]["type"] == "rich_text"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestSlack:
    @pytest.fixture
    def slack(self):
        with (
            patch("wrappers.slack.SlackAuth", return_value=MagicMock(token="xoxb-test")),
            patch(
                "wrappers.slack.config.get_slack_builds_channel",
                return_value="#builds",
            ),
            patch("wrappers.slack.WebClient") as mock_wc,
        ):
            s = Slack()
            s.client = mock_wc.return_value
            yield s

    # -- _get_ci_status_emoji -----------------------------------------------

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("failure", ":failed:"),
            ("FAILURE", ":failed:"),
            ("unstable", ":yellow-checkmark:"),
            ("success", ":done-circle-check:"),
            ("aborted", ":heavy_multiplication_x:"),
            ("unknown_status", ":question:"),
        ],
    )
    def test_get_ci_status_emoji(self, slack, status, expected):
        assert slack._get_ci_status_emoji(status) == expected

    # -- _get_user_tags -----------------------------------------------------

    def test_get_user_tags_returns_empty_when_all_success(self, slack):
        from models.dto import JenkinsChildJobAnalysisDTO, JenkinsJobAnalysisDTO, JenkinsJobDTO, JenkinsJobResultDTO

        job = JenkinsJobAnalysisDTO(
            job_result=JenkinsJobResultDTO(
                job=JenkinsJobDTO(
                    iib_version="2.11.0-1",
                    job_name="j",
                    build_number=1,
                    ocp_version="v4.21",
                    job_url="http://jenkins/j/1",
                ),
                result="SUCCESS",
                url="http://jenkins/j/1",
            ),
            summary="",
            child_jobs=[],
            html_report_url="",
        )
        assert slack._get_user_tags([job]) == {}

    def test_get_user_tags_returns_block_when_failure(self, slack):
        from models.dto import JenkinsJobAnalysisDTO, JenkinsJobDTO, JenkinsJobResultDTO

        job = JenkinsJobAnalysisDTO(
            job_result=JenkinsJobResultDTO(
                job=JenkinsJobDTO(
                    iib_version="2.11.0-1",
                    job_name="j",
                    build_number=1,
                    ocp_version="v4.21",
                    job_url="http://jenkins/j/1",
                ),
                result="FAILURE",
                url="http://jenkins/j/1",
            ),
            summary="",
            child_jobs=[],
            html_report_url="",
        )
        with patch(
            "wrappers.slack.config.get_slack_failure_mentions",
            return_value=["U12345"],
        ):
            block = slack._get_user_tags([job])
        assert block["type"] == "section"
        assert "<@U12345>" in block["text"]["text"]

    def test_get_user_tags_returns_empty_when_no_mentions_configured(self, slack):
        from models.dto import JenkinsJobAnalysisDTO, JenkinsJobDTO, JenkinsJobResultDTO

        job = JenkinsJobAnalysisDTO(
            job_result=JenkinsJobResultDTO(
                job=JenkinsJobDTO(
                    iib_version="2.11.0-1",
                    job_name="j",
                    build_number=1,
                    ocp_version="v4.21",
                    job_url="http://jenkins/j/1",
                ),
                result="FAILURE",
                url="http://jenkins/j/1",
            ),
            summary="",
            child_jobs=[],
            html_report_url="",
        )
        with patch(
            "wrappers.slack.config.get_slack_failure_mentions",
            return_value=[],
        ):
            assert slack._get_user_tags([job]) == {}

    # -- send_block ---------------------------------------------------------

    def test_send_block_without_thread_posts_message(self, slack):
        slack.client.chat_postMessage.return_value = {"ts": "111.222"}
        ts = slack.send_block([{"type": "divider"}], "#channel")
        slack.client.chat_postMessage.assert_called_once_with(
            channel="#channel", blocks=[{"type": "divider"}]
        )
        assert ts == "111.222"

    def test_send_block_with_thread_uses_thread_ts(self, slack):
        slack.client.chat_postMessage.return_value = {"ts": "333.444"}
        ts = slack.send_block([{"type": "divider"}], "#channel", ts="111.222")
        call_kwargs = slack.client.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "111.222"
        assert ts == "333.444"
