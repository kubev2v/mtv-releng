import logging
import time

import jenkins
from auth.auth import JenkinsAuth, StorageOffloadClusterAuth
from config import config
from models.dto import JenkinsJobDTO

logger = logging.getLogger(__name__)
import asyncio


class JenkinsManager:
    def __init__(self, url, verify_ssl=False):
        auth = JenkinsAuth()
        self.server = jenkins.Jenkins(
            url, username=auth.user, password=auth.token
        )
        # Handle self-signed certs
        if not verify_ssl:
            self.server._session.verify = config.get_root_cert_path()

    # returns queue_id
    def trigger_job(self, job_name: str, params=None) -> int:
        logger.info(f"Triggering jenkins job '{job_name}'")
        try:
            return int(self.server.build_job(job_name, parameters=params))
        except jenkins.NotFoundException as ex:
            logger.error(ex)
            return 0

    # returns build bumber
    async def wait_for_build_to_start(self, queue_id: int) -> int:
        logger.info(f"Waiting for build to start from queue '{queue_id}'")
        interval = config.get_jenkins_wait_refresh_seconds()
        while True:
            item = self.server.get_queue_item(queue_id)
            build_number = item.get("executable", {}).get("number")
            if build_number:
                logger.info(f"Build '{build_number}' started")
                return int(build_number)
            await asyncio.sleep(interval)

    # Get running job info
    async def get_job_info(self, job_name: str, build_number: int) -> dict:
        logger.info(f"Getting job info for {job_name}/{build_number}")
        info = self.server.get_build_info(job_name, build_number)
        logger.debug({"Job info": info})
        return info

    # Wait for build to finish
    async def wait_for_completion(
        self, job_name: str, build_number: int
    ) -> dict:
        logger.info(f"Waiting for build to finish {job_name}/{build_number}")
        interval = config.get_jenkins_wait_refresh_seconds()
        while True:
            info = await self.get_job_info(job_name, build_number)
            logger.info(
                f"Build {job_name}/{build_number} is still in progress"
            )
            logger.debug(f"Job info: {info}")
            if not info["building"]:
                return info
            await asyncio.sleep(interval)

    async def run_job(self, job_name: str, params=None) -> int:
        q_id = self.trigger_job(job_name, params)
        if not q_id:
            return 0
        bn = await self.wait_for_build_to_start(q_id)

        return bn

    def get_mtv_ci_args(self):
        return {
            "BRANCH": "master",
            "CLUSTER_NAME": "",
            "DEPLOY_MTV": "true",
            "GIT_BRANCH": "main",
            "IIB_NO": "",
            "MATRIX_TYPE": "",
            "MTV_API_TEST_GIT_USER": "RedHatQE",
            "MTV_SOURCE": "KONFLUX",
            "MTV_VERSION": "",
            "MTV_XY_VERSION": "",
            "NFS_SERVER_IP": "f02-h06-000-r640.rdu2.scalelab.redhat.com",
            "NFS_SHARE_PATH": "/home/nfsshare",
            "OCP_VERSION": "",
            "OCP_XY_VERSION": "",
            "OPENSHIFT_PYTHON_WRAPPER_GIT_BRANCH": "main",
            "PYTEST_EXTRA_PARAMS": "--tc=release_test:true --tc=target_ocp_version:{ocp}",
            "RC": True,
            "REMOTE_CLUSTER_NAME": "",
            "RUN_TESTS_IN_PARALLEL": "false",
            "CLEAN_CATALOG": "true",
            "ENABLE_JIRA": "true",
        }

    def get_storage_offload_args(
        self, mtv_version: str, ocp_version: str, iib: str
    ):
        return {
            "USE_USER_CLUSTER_CREDENTIALS": True,
            "CUSTOM_CLUSTER_NAME": "ocp-edge112",
            "OCP_API_URL": "https://api.ocp-edge112-0.lab.eng.tlv2.redhat.com:6443",
            "OCP_USERNAME": "kubeadmin",
            "OCP_PASSWORD": StorageOffloadClusterAuth().passwd,
            "OCP_VERSION": ocp_version,
            "DEPLOY_MTV": True,
            "IIB_NO": iib,
            "MTV_VERSION": mtv_version,
            "MTV_SOURCE": "KONFLUX",
            "COPYOFFLOAD_STORAGE_DEPLOY": "netapp-trident-deploy",
            "TRIDENT_STORAGE_ID": "rhos-netapp",
            "TRIDENT_BACKEND_SECRET_NAME": "trident-backend-secret",
            "TRIDENT_STORAGE_CLASS_NAME": "trident-storage-class",
            "SOURCE_PROVIDER": "vsphere-8.0.3-copyoffload-netapp-tlv",
            "STORAGE_CLASS": "trident-storage-class",
            "MARKER": "copyoffload",
            "PYTEST_PARAMS": '--tc=insecure_verify_skip:"true"',
            "PYTEST_EXTRA_PARAMS": '-k "not (TestCopyoffload2TbVmSnapshotsMigration or TestCopyoffloadLargeVmMigration or TestCopyoffloadScaleMigration)"',
            "GIT_BRANCH": "main",
            "MTV_API_TEST_GIT_USER": "RedHatQE",
            "SEND_EMAIL_ON_COMPLETION": True,
        }

    def get_test_release_gate_args(
        self, mtv_version: str, ocp_version: str, iib: str
    ) -> dict:
        cluster_mappings = config.get_cluster_mappings()
        args = self.get_mtv_ci_args()
        args["IIB_NO"] = iib
        args["MTV_VERSION"] = mtv_version
        args["MTV_XY_VERSION"] = ".".join(mtv_version.split(".")[:2])
        args["OCP_VERSION"] = ocp_version
        args["OCP_XY_VERSION"] = ocp_version
        args["PYTEST_EXTRA_PARAMS"] = args["PYTEST_EXTRA_PARAMS"].replace(
            "{ocp}", ocp_version
        )
        args["MATRIX_TYPE"] = "RELEASE"
        cluster = cluster_mappings.get(ocp_version, "")
        if not cluster:
            raise ValueError(
                f"OCP {ocp_version} not in cluster mappings {cluster_mappings}"
            )
        if cluster.lower() == "none":
            logger.warning(
                f"OCP {ocp_version} disabled in cluster mappings {cluster_mappings}"
            )
            return {}
        args["CLUSTER_NAME"] = cluster
        args["REMOTE_CLUSTER_NAME"] = cluster
        return args

    def get_test_release_non_gate_args(
        self, mtv_version: str, ocp_version: str, iib: str
    ) -> dict:
        cluster_mappings = config.get_cluster_mappings()
        args = self.get_mtv_ci_args()
        args["IIB_NO"] = iib
        args["MTV_VERSION"] = mtv_version
        args["MTV_XY_VERSION"] = ".".join(mtv_version.split(".")[:2])
        args["OCP_VERSION"] = ocp_version
        args["OCP_XY_VERSION"] = ocp_version
        args["PYTEST_EXTRA_PARAMS"] = args["PYTEST_EXTRA_PARAMS"].replace(
            "{ocp}", ocp_version
        )
        args["MATRIX_TYPE"] = "TIER1"
        cluster = cluster_mappings.get(ocp_version, "")
        if not cluster:
            raise ValueError(
                f"OCP {ocp_version} not in cluster mappings {cluster_mappings}"
            )
        if cluster.lower() == "none":
            logger.warning(
                f"OCP {ocp_version} disabled in cluster mappings {cluster_mappings}"
            )
            return {}
        args["CLUSTER_NAME"] = cluster
        args["REMOTE_CLUSTER_NAME"] = cluster
        return args

    def get_ui_testing_args(
        self, mtv_version: str, iib: str, target_cluster: str
    ):
        return {
            "BRANCH": "master",
            "CLUSTER_NAME": target_cluster,
            "DEPLOY_MTV": True,
            "IIB_NO": iib,
            "MTV_SOURCE": "KONFLUX",
            "MTV_VERSION": mtv_version,
            "RC": True,
            "CLEAN_CATALOG": True,
            "TEST_ARGS": "--grep=@downstream",
            "UI_TEST_IMAGE": "quay.io/kubev2v/forklift-ui-tests:latest",
            "VSPHERE_PROVIDER": "vsphere-8.0.1",
            "AGENT_RESOURCES": "LARGE",
            "UPLOAD_TO_RP": False,
        }

    async def trigger_release_gate(
        self, mtv_version: str, ocp_version: str, iib: str
    ) -> dict:
        ci_args = self.get_test_release_gate_args(
            mtv_version, ocp_version.replace("v", ""), iib
        )
        if not ci_args:
            logger.warning(
                f"Missing arguments, can't trigger a job for {ocp_version}/{mtv_version}"
            )
            return {}
        mtv_xy = ".".join(mtv_version.split(".")[:2])
        ocp_wv = ocp_version.replace("v", "")

        job_name = f"mtv-{mtv_xy}-ocp-{ocp_wv}-test-release-gate"
        job_number = await self.run_job(job_name, ci_args)
        if job_number:
            return {"job_name": job_name, "job_number": job_number}
        else:
            return {}

    async def trigger_release_non_gate(
        self, mtv_version: str, ocp_version: str, iib: str
    ) -> dict:
        ci_args = self.get_test_release_non_gate_args(
            mtv_version, ocp_version.replace("v", ""), iib
        )
        if not ci_args:
            logger.warning(
                f"Missing arguments, can't trigger a job for {ocp_version}/{mtv_version}"
            )
            return {}
        mtv_xy = ".".join(mtv_version.split(".")[:2])
        ocp_wv = ocp_version.replace("v", "")

        job_name = f"mtv-{mtv_xy}-ocp-{ocp_wv}-test-release-non-gate"
        job_number = await self.run_job(job_name, ci_args)
        if job_number:
            return {"job_name": job_name, "job_number": job_number}
        else:
            return {}

    async def trigger_storage_offload(
        self, mtv_version: str, iib: str, ocp_version: str = "v4.20"
    ):
        mtv_xy = ".".join(mtv_version.split(".")[:2])
        ocp_wv = ocp_version.replace("v", "")

        ci_args = self.get_storage_offload_args(mtv_version, ocp_version, iib)
        if not ci_args:
            logger.warning(
                f"Missing arguments, can't trigger a job for {ocp_version}/{mtv_version}"
            )
            return {}

        job_name = f"mtv-{mtv_xy}-ocp-{ocp_wv}-copyoffload-tests"
        job_number = await self.run_job(job_name, ci_args)
        if job_number:
            return {"job_name": job_name, "job_number": job_number}
        else:
            return {}

    async def trigger_ui_testing(
        self, mtv_version: str, ocp_versions: list[str], iib: str
    ):
        cluster_mapping = config.get_ui_cluster_mapping()
        if not cluster_mapping:
            logger.error("Couldn't get UI cluster mapping")
            return {}

        mappings = cluster_mapping.items()
        ocps = [ver.replace("v", "") for ver in ocp_versions]
        target_cluster = ""
        target_ocp = ""
        for cluster_name, cluster_version in mappings:
            if cluster_version in ocps:
                target_cluster = cluster_name
                target_ocp = cluster_version
                break
        if not target_cluster:
            logger.warning(
                f"MTV {mtv_version} does not have supported UI cluster in config, skipping tests."
            )
            return {}

        ci_args = self.get_ui_testing_args(mtv_version, iib, target_cluster)
        if not ci_args:
            logger.warning(
                f"Missing arguments, can't trigger a UI test job for {mtv_version} on {target_cluster}"
            )
            return {}

        job_name = "dev-mtv-deploy-and-ui-tests"
        job_number = await self.run_job(job_name, ci_args)
        if job_number:
            return {
                "job_name": job_name,
                "job_number": job_number,
                "target_ocp": target_ocp,
            }
        else:
            return {}
