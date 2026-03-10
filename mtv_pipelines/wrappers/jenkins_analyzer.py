import json
import logging

import requests
from config import config
from models.dto import (
    JenkinsChildJobAnalysisDTO,
    JenkinsJobAnalysisDTO,
    JenkinsJobResultDTO,
)

logger = logging.getLogger(__name__)


class JenkinsAnalyzer:
    def analyze_job(
        self, job_result: JenkinsJobResultDTO
    ) -> JenkinsJobAnalysisDTO:
        logger.info(f"Analyzing job result for {job_result.url}")
        resp = requests.post(
            f"{config.get_jenkins_analyzer_url().rstrip("/")}/analyze?sync=true",
            headers={"Content-Type": "application/json"},
            json={
                "job_name": job_result.job.job_name,
                "build_number": job_result.job.build_number,
                "ai_provider": "cursor",
                "ai_model": "gpt-5.4-xhigh-fast",
            },
            verify=False,
        )
        resp.raise_for_status()
        data = json.loads(resp.content)
        analysis = self._process_data(data, job_result)

        return analysis

    def _process_data(
        self, data: dict, job_result: JenkinsJobResultDTO
    ) -> JenkinsJobAnalysisDTO:
        children = []
        for child in data["child_job_analyses"]:
            children.append(
                JenkinsChildJobAnalysisDTO(
                    job_name=child["job_name"],
                    build_number=child["build_number"],
                    job_url=child["jenkins_url"],
                    summary=child["summary"],
                )
            )
        return JenkinsJobAnalysisDTO(
            job_result=job_result,
            summary=data["summary"],
            child_jobs=children,
            html_report_url=data["html_report_url"],
        )

    def _prepare_output_for_mrkdwn(self, output: dict):
        logger.debug(f"Sanitizing output for slack mrkdwn: {output}")

        return output
