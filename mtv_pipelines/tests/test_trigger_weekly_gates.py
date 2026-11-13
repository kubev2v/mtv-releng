"""Tests for Saturday trigger_weekly_gates pipeline helpers."""

import asyncio
import json
from argparse import Namespace
from unittest.mock import AsyncMock, MagicMock, patch

from models.dto import (
    CollectorDTO,
    EmptyDTO,
    JenkinsJobAnalysisDTO,
    JenkinsJobDTO,
    JenkinsJobResultDTO,
    SlackBuildMessageTSDTO,
)
from tasks.latest_iib_state import upsert_latest

from pipelines import trigger_weekly_gates as pipeline


def run_task(task, data, args):
    async def _inner():
        async with asyncio.TaskGroup() as tg:
            return await task.run(data, args, tg)

    return asyncio.run(_inner())


def _args(**kwargs):
    values = {"skip_slack": False, "dry_run": False}
    values.update(kwargs)
    return Namespace(**values)


def _write_pointer(path, iib="forklift-fbc-prod-v50:on-pr-abc"):
    upsert_latest(
        path,
        mtv_xy="2.12",
        mtv_version="2.12.6",
        iib=iib,
        recorded_at="2026-09-17T14:02:00Z",
    )


class TestTriggerWeeklyGateJobs:
    def test_missing_pointer_does_not_trigger(self, tmp_path):
        path = tmp_path / "latest_iib.json"
        with (
            patch(
                "pipelines.trigger_weekly_gates.config.get_latest_iib_state_path",
                return_value=str(path),
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_tier1_jobs",
                return_value={"2.12": {"ocp_version": "4.22"}},
            ),
            patch("pipelines.trigger_weekly_gates.JenkinsManager") as mock_jm,
        ):
            jobs = run_task(
                pipeline.trigger_weekly_gate_jobs, EmptyDTO(), _args()
            )
        assert jobs == []
        mock_jm.assert_not_called()

    def test_rewrites_iib_for_target_ocp(self, tmp_path):
        path = tmp_path / "latest_iib.json"
        _write_pointer(path)
        mgr = MagicMock()
        mgr.trigger_tier1 = AsyncMock(
            return_value={
                "job_name": "mtv-2.12-ocp-4.22-test-tier1",
                "job_number": 7,
            }
        )
        mgr.get_job_info = AsyncMock(
            return_value={"url": "http://jenkins/job/t/7/"}
        )
        with (
            patch(
                "pipelines.trigger_weekly_gates.config.get_latest_iib_state_path",
                return_value=str(path),
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_tier1_jobs",
                return_value={"2.12": {"ocp_version": "4.22"}},
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_jenkins_url",
                return_value="http://jenkins",
            ),
            patch(
                "pipelines.trigger_weekly_gates.JenkinsManager",
                return_value=mgr,
            ),
        ):
            jobs = run_task(
                pipeline.trigger_weekly_gate_jobs, EmptyDTO(), _args()
            )
        mgr.trigger_tier1.assert_awaited_once_with(
            "2.12.6",
            "4.22",
            "forklift-fbc-prod-v422:on-pr-abc",
        )
        assert len(jobs) == 1
        assert jobs[0].job_name == "mtv-2.12-ocp-4.22-test-tier1"
        assert jobs[0].iib_version == "2.12.6"
        assert json.loads(path.read_text())["2.12"]["last_tier1_iib"] == (
            "forklift-fbc-prod-v50:on-pr-abc"
        )

    def test_missing_cluster_mapping_skips_target_and_continues(self, tmp_path):
        path = tmp_path / "latest_iib.json"
        upsert_latest(
            path,
            mtv_xy="2.12",
            mtv_version="2.12.6",
            iib="forklift-fbc-prod-v50:on-pr-abc",
            recorded_at="2026-09-17T14:02:00Z",
        )
        upsert_latest(
            path,
            mtv_xy="5.0",
            mtv_version="5.0.0",
            iib="forklift-fbc-prod-v50:on-pr-xyz",
            recorded_at="2026-09-17T14:02:00Z",
        )
        mgr = MagicMock()
        mgr.trigger_tier1 = AsyncMock(
            side_effect=[
                ValueError("no cluster mapping for 4.22"),
                {"job_name": "mtv-5.0-ocp-5.0-test-tier1", "job_number": 3},
            ]
        )
        mgr.get_job_info = AsyncMock(
            return_value={"url": "http://jenkins/job/t/3/"}
        )
        with (
            patch(
                "pipelines.trigger_weekly_gates.config.get_latest_iib_state_path",
                return_value=str(path),
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_tier1_jobs",
                return_value={
                    "2.12": {"ocp_version": "4.22"},
                    "5.0": {"ocp_version": "5.0"},
                },
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_hyperv_gate_jobs",
                return_value={},
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_jenkins_url",
                return_value="http://jenkins",
            ),
            patch(
                "pipelines.trigger_weekly_gates.JenkinsManager",
                return_value=mgr,
            ),
        ):
            jobs = run_task(
                pipeline.trigger_weekly_gate_jobs, EmptyDTO(), _args()
            )
        assert len(jobs) == 1
        assert jobs[0].job_name == "mtv-5.0-ocp-5.0-test-tier1"
        data = json.loads(path.read_text())
        assert data["2.12"].get("last_tier1_iib") is None
        assert data["5.0"]["last_tier1_iib"] == (
            "forklift-fbc-prod-v50:on-pr-xyz"
        )

    def test_marks_triggered_even_if_get_job_info_fails(self, tmp_path):
        path = tmp_path / "latest_iib.json"
        _write_pointer(path)
        mgr = MagicMock()
        mgr.trigger_tier1 = AsyncMock(
            return_value={
                "job_name": "mtv-2.12-ocp-4.22-test-tier1",
                "job_number": 7,
            }
        )
        mgr.get_job_info = AsyncMock(
            side_effect=RuntimeError("transient Jenkins API error")
        )
        with (
            patch(
                "pipelines.trigger_weekly_gates.config.get_latest_iib_state_path",
                return_value=str(path),
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_tier1_jobs",
                return_value={"2.12": {"ocp_version": "4.22"}},
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_jenkins_url",
                return_value="http://jenkins",
            ),
            patch(
                "pipelines.trigger_weekly_gates.JenkinsManager",
                return_value=mgr,
            ),
        ):
            jobs = run_task(
                pipeline.trigger_weekly_gate_jobs, EmptyDTO(), _args()
            )
        assert len(jobs) == 1
        assert jobs[0].job_name == "mtv-2.12-ocp-4.22-test-tier1"
        assert jobs[0].build_number == 7
        assert jobs[0].job_url == ""
        assert json.loads(path.read_text())["2.12"]["last_tier1_iib"] == (
            "forklift-fbc-prod-v50:on-pr-abc"
        )

    def test_dry_run_does_not_call_jenkins(self, tmp_path):
        path = tmp_path / "latest_iib.json"
        _write_pointer(path)
        with (
            patch(
                "pipelines.trigger_weekly_gates.config.get_latest_iib_state_path",
                return_value=str(path),
            ),
            patch(
                "pipelines.trigger_weekly_gates.config.get_tier1_jobs",
                return_value={"2.12": {"ocp_version": "4.22"}},
            ),
            patch("pipelines.trigger_weekly_gates.JenkinsManager") as mock_jm,
        ):
            jobs = run_task(
                pipeline.trigger_weekly_gate_jobs,
                EmptyDTO(),
                _args(dry_run=True),
            )
        assert jobs == []
        mock_jm.assert_not_called()
        assert json.loads(path.read_text())["2.12"].get("last_tier1_iib") is None


class TestSendWeeklyGateSlack:
    def _job(self):
        return JenkinsJobDTO(
            iib_version="2.12.6",
            job_name="mtv-2.12-ocp-4.22-test-tier1",
            build_number=7,
            ocp_version="4.22",
            job_url="http://jenkins/job/t/7/",
        )

    def test_skip_slack_returns_empty(self):
        ts = run_task(
            pipeline.send_weekly_gate_slack_triggered,
            [self._job()],
            _args(skip_slack=True),
        )
        assert ts == []

    def test_posts_parent_then_triggered_jobs(self, tmp_path):
        path = tmp_path / "latest_iib.json"
        _write_pointer(path)
        slack = MagicMock()
        slack.send_weekly_gates_run.return_value = SlackBuildMessageTSDTO(
            iib_version="2.12.6", timestamp="1.2"
        )
        with (
            patch(
                "pipelines.trigger_weekly_gates.config.get_latest_iib_state_path",
                return_value=str(path),
            ),
            patch("pipelines.trigger_weekly_gates.Slack", return_value=slack),
        ):
            result = run_task(
                pipeline.send_weekly_gate_slack_triggered,
                [self._job()],
                _args(),
            )
        slack.send_weekly_gates_run.assert_called_once_with(
            "2.12.6",
            "4.22",
            "forklift-fbc-prod-v422:on-pr-abc",
        )
        slack.send_triggered_jobs.assert_called_once()
        assert result[0].timestamp == "1.2"


class TestSendWeeklyGateCi:
    def test_skip_slack_does_not_post_status(self):
        slack = MagicMock()
        jobs = [
            JenkinsJobAnalysisDTO(
                job_result=JenkinsJobResultDTO(
                    job=JenkinsJobDTO(
                        iib_version="2.12.6",
                        job_name="mtv-2.12-ocp-4.22-test-tier1",
                        build_number=7,
                        ocp_version="4.22",
                        job_url="http://jenkins/job/t/7/",
                    ),
                    result="SUCCESS",
                    url="http://jenkins/job/t/7/",
                ),
                summary="",
                child_jobs=[],
                html_report_url="",
            )
        ]
        timestamps = [
            SlackBuildMessageTSDTO(iib_version="2.12.6", timestamp="1.2")
        ]
        data = CollectorDTO(
            task_outputs={
                pipeline.analyze_weekly_gate_jobs.name: jobs,
                pipeline.send_weekly_gate_slack_triggered.name: timestamps,
            }
        )
        with patch("pipelines.trigger_weekly_gates.Slack", return_value=slack):
            run_task(
                pipeline.send_weekly_gate_slack_ci,
                data,
                _args(skip_slack=True),
            )
        slack.send_ci_status.assert_not_called()

    def test_posts_ci_status_on_parent_thread(self):
        slack = MagicMock()
        jobs = [
            JenkinsJobAnalysisDTO(
                job_result=JenkinsJobResultDTO(
                    job=JenkinsJobDTO(
                        iib_version="2.12.6",
                        job_name="mtv-2.12-ocp-4.22-test-tier1",
                        build_number=7,
                        ocp_version="4.22",
                        job_url="http://jenkins/job/t/7/",
                    ),
                    result="SUCCESS",
                    url="http://jenkins/job/t/7/",
                ),
                summary="",
                child_jobs=[],
                html_report_url="",
            )
        ]
        timestamps = [
            SlackBuildMessageTSDTO(iib_version="2.12.6", timestamp="1.2")
        ]
        data = CollectorDTO(
            task_outputs={
                pipeline.analyze_weekly_gate_jobs.name: jobs,
                pipeline.send_weekly_gate_slack_triggered.name: timestamps,
            }
        )
        with patch("pipelines.trigger_weekly_gates.Slack", return_value=slack):
            run_task(pipeline.send_weekly_gate_slack_ci, data, _args())
        slack.send_ci_status.assert_called_once()
        posted_jobs, ts = slack.send_ci_status.call_args.args
        assert posted_jobs == jobs
        assert ts.timestamp == "1.2"
