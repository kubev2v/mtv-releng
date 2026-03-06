import yaml

CONFIG = "mtv_pipelines/config/config.yaml"


def read_config() -> dict:
    with open(CONFIG, "rb") as f:
        data = yaml.safe_load(f)
    if data:
        return data
    raise RuntimeError("Failed to read config file")


def _parse_simple(var_name: str):
    conf = read_config()
    var = conf.get(var_name)
    if var:
        return var
    raise RuntimeError(f'Couldn\'t find "{var_name}" in config')


def get_name() -> dict:
    return _parse_simple("operator_name")


def get_mtv_versions() -> dict:
    return _parse_simple("versions")


def get_mtv_repositories() -> dict:
    return _parse_simple("repositories")


def get_mtv_cutoff_version() -> str:
    return _parse_simple("mtv_cutoff_version")


def get_mtv_fbc_repo() -> str:
    return _parse_simple("fbc_repo")


def get_mtv_branches() -> list[str]:
    return _parse_simple("branches")


def get_git_name() -> str:
    return _parse_simple("git_name")


def get_git_email() -> str:
    return _parse_simple("git_email")


def get_git_label() -> str:
    return _parse_simple("git_label")


def get_fbc_component_url() -> str:
    return _parse_simple("fbc_component_url")


def get_fbc_component_name() -> str:
    return _parse_simple("fbc_component_name")


def get_cpe_version_init() -> str:
    return _parse_simple("mtv_cpe_version_init")


def get_cmp_mappings() -> dict:
    return _parse_simple("cmp_mappings")


def get_raw_mappings() -> dict:
    return _parse_simple("raw_mappings")


def get_dev_preview_namespace() -> str:
    return _parse_simple("dev_preview_namespace")


def get_release_namespace() -> str:
    return _parse_simple("release_namespace")


def get_iib_max_commits() -> int:
    return _parse_simple("iib_max_commits")


def get_fbc_pr_max_retries() -> int:
    return _parse_simple("fbc_pr_max_retries")


def get_fbc_pr_refresh_seconds() -> int:
    return _parse_simple("fbc_pr_refresh_seconds")


def get_slack_api() -> str:
    return _parse_simple("slack_api")


def get_slack_builds_channel() -> str:
    return _parse_simple("slack_builds_channel")


def get_slack_errors_channel() -> str:
    return _parse_simple("slack_errors_channel")


def get_templates_dir() -> str:
    return _parse_simple("templates_dir")


def get_jira_url() -> str:
    return _parse_simple("jira_issues")


def get_commit_character_limit() -> int:
    return _parse_simple("commit_character_limit")


def get_cluster_mappings() -> dict:
    return _parse_simple("cluster_mappings")


def get_root_cert_path() -> str:
    return _parse_simple("root_cert_path")


def get_jenkins_url() -> str:
    return _parse_simple("jenkins_url")


def get_allowed_project_keys() -> list[str]:
    return _parse_simple("allowed_project_keys")


def get_jenkins_wait_refresh_seconds() -> int:
    return _parse_simple("jenkins_wait_refresh_seconds")


def get_jenkins_analyzer_url() -> str:
    return _parse_simple("jenkins_analyzer_url")


def get_slack_failure_mentions() -> list[str]:
    return _parse_simple("slack_failure_mentions")


def get_db_path() -> str:
    return _parse_simple("db_path")
