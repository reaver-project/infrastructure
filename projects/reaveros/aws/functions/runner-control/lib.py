import datetime
import re
import secrets


def require_match(value, expression, description):
    if not isinstance(value, str) or re.fullmatch(expression, value) is None:
        raise ValueError(f"invalid {description}")
    return value


def validate_repository(repository, allowed_repositories):
    require_match(repository, r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "repository")
    if repository not in allowed_repositories:
        raise ValueError("repository is not allowed to provision runners")


def runner_identity(event, parameter_prefix, parameter_nonce=None):
    runner_key = require_match(event.get("runner_key"), r"[a-z0-9-]{1,48}", "runner key")
    run_id = require_match(str(event.get("github_run_id")), r"[0-9]+", "run ID")
    run_attempt = require_match(
        str(event.get("github_run_attempt")),
        r"[0-9]+",
        "run attempt",
    )
    if parameter_nonce is None:
        parameter_nonce = secrets.token_hex(16)
    require_match(parameter_nonce, r"[0-9a-f]{32}", "parameter nonce")
    return {
        "jit_parameter": (
            f"{parameter_prefix}{run_id}-{run_attempt}-{runner_key}-{parameter_nonce}"
        ),
        "run_id": run_id,
        "runner_name": f"reaveros-{run_id}-{run_attempt}-{runner_key}",
    }


def runner_tag_specifications(
    identity,
    repository,
    runner_size,
    runner_profile,
    instance_name,
):
    common_tags = [
        {"Key": "Project", "Value": "ReaverOS"},
        {"Key": "ReaverOSPurpose", "Value": "GitHubActionsRunner"},
    ]
    return [
        {
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": instance_name},
                *common_tags,
                {"Key": "GitHubRepository", "Value": repository},
                {"Key": "GitHubRunId", "Value": identity["run_id"]},
                {"Key": "GitHubRunnerId", "Value": str(identity["runner_id"])},
                {"Key": "GitHubRunnerName", "Value": identity["runner_name"]},
                {
                    "Key": "ReaverOSRunnerParameter",
                    "Value": identity["jit_parameter"],
                },
                {"Key": "ReaverOSRunnerProfile", "Value": runner_profile},
                {"Key": "ReaverOSRunnerSize", "Value": runner_size},
            ],
        },
        {
            "ResourceType": "volume",
            "Tags": common_tags,
        },
    ]


def runner_cleanup(instance, parameter_prefix):
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    jit_parameter = tags.get("ReaverOSRunnerParameter")
    if not isinstance(jit_parameter, str) or not jit_parameter.startswith(parameter_prefix):
        jit_parameter = None
    runner_id = tags.get("GitHubRunnerId")
    if not isinstance(runner_id, str) or not runner_id.isdecimal():
        runner_id = None
    else:
        runner_id = int(runner_id)
    return {
        "instance_id": instance["InstanceId"],
        "jit_parameter": jit_parameter,
        "runner_id": runner_id,
    }


def expired_runner_cleanup(instances, now, maximum_age_minutes, parameter_prefix):
    cutoff = now - datetime.timedelta(minutes=maximum_age_minutes)
    return [
        runner_cleanup(instance, parameter_prefix)
        for instance in instances
        if instance["LaunchTime"] < cutoff
    ]
