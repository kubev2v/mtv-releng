from enum import Enum, StrEnum, auto
from tempfile import TemporaryDirectory
from typing import Any

from models.bundle import Bundle
from models.fbc_repo import FBCRepo
from models.iib import IIB
from pydantic import BaseModel, ConfigDict
from semver import Version


class EmptyDTO(BaseModel):
    pass


class CollectorDTO(BaseModel):
    task_outputs: dict[str, Any]  # {"task_name": task_out_obj}


class VersionDTO(BaseModel):
    major: int
    minor: int
    patch: int
    prerelease: int | None

    model_config = ConfigDict(from_attributes=True)

    def to_version(self):
        return Version(
            major=self.major,
            minor=self.minor,
            patch=self.patch,
            prerelease=self.prerelease,
        )


class MTVBranchVersionDTO(BaseModel):
    branch: str  # main, release-2.10...
    version: VersionDTO  # Version(2.10.0), Version(2.11.3)...


class MTVRepoBranchVersionsDTO(BaseModel):
    repo: str  # forklift, forklift-console-plugin, forklift-must-gather
    branch_versions: list[MTVBranchVersionDTO]


class MTVVersionsDTO(BaseModel):
    versions: list[MTVRepoBranchVersionsDTO]


class BundleDTO(BaseModel):
    url: str
    digest: str
    arch: str
    version: VersionDTO
    commit: str
    ocps: list[str]
    channel: str
    cpe: str
    rhel: int | None

    model_config = ConfigDict(from_attributes=True)

    def to_bundle(self):
        b = Bundle(self.url)
        b.digest = self.digest
        b.arch = self.arch
        b.version = self.version.to_version()
        b.commit = self.commit
        b.ocps = self.ocps
        b.channel = self.channel
        b.cpe = self.cpe
        b.rhel = self.rhel
        return b


class BundlesDTO(BaseModel):
    bundles: list[BundleDTO]


class IIBDTO(BaseModel):
    version: VersionDTO
    url: str


class FBCRepoDTO(BaseModel):
    tmp_dir: TemporaryDirectory
    url: str
    target: str
    for_bundle: Bundle
    previous_commit: dict
    previous_iib_version: Version | None
    current_iib_version: Version | None
    pr_url: str
    previous_iib: IIB | None
    current_iib: IIB | None

    model_config = ConfigDict(
        from_attributes=True, arbitrary_types_allowed=True
    )

    def to_fbc_repo(self):
        f = FBCRepo()
        f.tmp_dir = self.tmp_dir
        f.for_bundle = self.for_bundle
        f.previous_commit = self.previous_commit
        f.previous_iib_version = self.previous_iib_version
        f.current_iib_version = self.current_iib_version
        f.pr_url = self.pr_url
        f.previous_iib = self.previous_iib
        f.current_iib = self.current_iib
        return f


class FBCReposDTO(BaseModel):
    repos: list[FBCRepoDTO]


class CheckStatus(StrEnum):
    QUEUED = auto()
    FAILURE = auto()
    IN_PROGRESS = auto()
    SUCCESS = auto()
    NOT_FOUND = auto()
    CANCELLED = auto()


class CheckStatusDTO(BaseModel):
    name: str
    status: CheckStatus


class CommitDTO(BaseModel):
    sha: str
    msg: str
    date: str
    author: str
    issues: list[str] = []


class RepoCommitDTO(BaseModel):
    repo: str
    version: str
    sha: str


class RepoDiffDTO(BaseModel):
    repo: str
    version: str
    diff: list[CommitDTO]


class SlackBuildMessageDTO(BaseModel):
    iib_version: str
    ocp_urls: dict[str, str]
    bundle_url: str
    snapshot: str
    commits: dict[str, str]
    changes: list[RepoDiffDTO]
    prev_build: tuple[str, str]


class SlackBuildMessageTSDTO(BaseModel):
    iib_version: str
    timestamp: str


class JenkinsJobDTO(BaseModel):
    iib_version: str
    job_name: str
    build_number: int
    ocp_version: str
    job_url: str


class JenkinsJobResultDTO(BaseModel):
    job: JenkinsJobDTO
    result: str
    url: str


class JenkinsChildJobAnalysisDTO(BaseModel):
    job_name: str
    build_number: int
    job_url: str
    summary: str


class JenkinsJobAnalysisDTO(BaseModel):
    job_result: JenkinsJobResultDTO
    summary: str
    child_jobs: list[JenkinsChildJobAnalysisDTO]
    html_report_url: str


class JenkinsJobResultToBuildTSDTO(BaseModel):
    jobs: list[JenkinsJobResultDTO]
    timestamp: SlackBuildMessageTSDTO


class ReversionResultDTO(BaseModel):
    origin: str
    branch: str
    old_version: str
    new_version: str
    pr_url: str = ""
    skipped: bool = False
    skip_reason: str = ""


class BtrfsSyncResultDTO(BaseModel):
    branch: str
    created_from_main: bool = False
    pushed: bool = False
    dry_run: bool = False
    skipped: bool = False
    skip_reason: str = ""


class BundleSyncResultDTO(BaseModel):
    version: str
    target_branch: str
    snapshot: str = ""
    components_updated: int = 0
    pr_url: str = ""
    existing_branch: str = ""
    dry_run: bool = False
    skipped: bool = False
    skip_reason: str = ""


class BranchingResultDTO(BaseModel):
    origin: str
    version: str
    release_branch: str
    cf_branch: str
    release_branch_created: bool = False
    pr_url: str = ""
    dry_run: bool = False
    skipped: bool = False
    skip_reason: str = ""


class CveTicketDTO(BaseModel):
    key: str
    summary: str
    branch: str = ""  # release-2.11 (parsed from the [mtv-2.11] suffix)
    cve: str = ""
    component: str = ""  # pscomponent, e.g. mtv-api-rhel9
    due_date: str = ""
    status: str = ""
    labels: list[str] = []
    description: str = ""
    embargoed: bool = False


class CveTicketsDTO(BaseModel):
    tickets: list[CveTicketDTO] = []
    skipped: bool = False
    skip_reason: str = ""


class CveFindingDTO(BaseModel):
    ticket: CveTicketDTO
    category: str
    evidence: list[str] = []
    image: str = ""
    label: str = ""
    comment: str = ""
    applied: bool = False


class CveFindingsDTO(BaseModel):
    findings: list[CveFindingDTO]


class CveTriageResultDTO(BaseModel):
    counts: dict[str, int] = {}
    report_path: str = ""
    applied: int = 0
    dry_run: bool = False
    skipped: bool = False
    skip_reason: str = ""


class KonfluxStreamResultDTO(BaseModel):
    version: str
    version_name: str
    branch: str = ""
    prod_stream_file: str = ""
    btrfs_updated: bool = False
    rpa_files_created: int = 0
    pushed: bool = False
    pr_url: str = ""
    dry_run: bool = False
    skipped: bool = False
    skip_reason: str = ""
