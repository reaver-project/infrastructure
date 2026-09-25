import base64
import calendar
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from github_app import GitHubRequestError, create_app_jwt, request
from lib import (
    expired_runner_cleanup,
    require_match,
    runner_cleanup,
    runner_identity,
    runner_tag_specifications,
    validate_repository,
    workflow_reference,
)

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")
cached_github_token = None
cached_github_token_expiry = 0


def github_request(path, token, method="GET", body=None):
    return request(
        path,
        token,
        method,
        body,
        user_agent="reaver-project-runner-controller",
    )


def github_token():
    global cached_github_token
    global cached_github_token_expiry

    if cached_github_token_expiry > time.time() + 60:
        return cached_github_token
    response = secrets.get_secret_value(SecretId=os.environ["GITHUB_APP_SECRET_ID"])
    credentials = json.loads(response["SecretString"])
    installation_token = github_request(
        f"/app/installations/{credentials['installation_id']}/access_tokens",
        create_app_jwt(credentials),
        "POST",
        {},
    )
    cached_github_token = installation_token["token"]
    cached_github_token_expiry = calendar.timegm(
        time.strptime(
            installation_token["expires_at"],
            "%Y-%m-%dT%H:%M:%SZ",
        )
    )
    return cached_github_token


def parameter_expiration_policy(maximum_age_minutes, now=None):
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    expiration = now + datetime.timedelta(minutes=maximum_age_minutes + 60)
    return json.dumps(
        [
            {
                "Type": "Expiration",
                "Version": "1.0",
                "Attributes": {
                    "Timestamp": expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            }
        ],
        separators=(",", ":"),
    )


def runner_instances(instance_ids=None):
    arguments = {
        "Filters": [
            {
                "Name": "tag:ReaverOSPurpose",
                "Values": ["GitHubActionsRunner"],
            }
        ],
    }
    if instance_ids is not None:
        arguments["InstanceIds"] = instance_ids
    instances = []
    while True:
        response = ec2.describe_instances(**arguments)
        instances.extend(
            instance
            for reservation in response["Reservations"]
            for instance in reservation["Instances"]
        )
        next_token = response.get("NextToken")
        if next_token is None:
            return instances
        arguments["NextToken"] = next_token


def restricted_workflows(token, repository):
    organization = os.environ["GITHUB_ORGANIZATION"]
    group_id = int(os.environ["GITHUB_RUNNER_GROUP_ID"])
    path = f"/orgs/{organization}/actions/runner-groups/{group_id}"
    group = github_request(path, token)
    selected = group.get("selected_workflows") if isinstance(group, dict) else None
    if (
        not isinstance(selected, list)
        or not all(isinstance(reference, str) for reference in selected)
        or group.get("restricted_to_workflows") is not True
        or group.get("name") != "reaveros"
    ):
        raise ValueError("runner group is not restricted to ReaverOS workflows")
    prefix = f"{repository}/.github/workflows/aws-runner.yml@"
    for reference in selected:
        if not reference.startswith(prefix):
            raise ValueError("runner group contains an unexpected workflow")
        workflow_reference(repository, reference.removeprefix(prefix))

    return path, set(selected)


def ensure_workflow_access(token, repository, source_ref):
    path, selected = restricted_workflows(token, repository)
    requested = {
        *selected,
        workflow_reference(repository, "refs/heads/main"),
        workflow_reference(repository, source_ref),
    }
    if requested != selected:
        result = github_request(
            path,
            token,
            "PATCH",
            {"selected_workflows": sorted(requested)},
        )
        if not isinstance(result, dict) or set(result.get("selected_workflows", [])) != requested:
            raise RuntimeError("runner group did not retain the approved workflow references")


def prune_workflow_access(repository, protected_refs=()):
    # ReaverOS is public, so this read does not expand the Runner App installation.
    references = github_request(
        f"/repos/{repository}/git/matching-refs/heads/pull-request/",
        None,
    )
    if not isinstance(references, list):
        raise ValueError("GitHub did not return copied CI refs")
    active = {workflow_reference(repository, "refs/heads/main")}
    for source_ref in protected_refs:
        active.add(workflow_reference(repository, source_ref))
    for reference in references:
        source_ref = reference.get("ref") if isinstance(reference, dict) else None
        active.add(workflow_reference(repository, source_ref))

    token = github_token()
    path, selected = restricted_workflows(token, repository)
    requested = selected & active
    requested.add(workflow_reference(repository, "refs/heads/main"))
    if requested != selected:
        result = github_request(
            path,
            token,
            "PATCH",
            {"selected_workflows": sorted(requested)},
        )
        if not isinstance(result, dict) or set(result.get("selected_workflows", [])) != requested:
            raise RuntimeError("runner group did not prune revoked workflow references")


def launch(event):
    repository = event.get("repository")
    validate_repository(
        repository,
        set(os.environ["ALLOWED_REPOSITORIES"].split(",")),
    )
    source_ref = event.get("source_ref")
    workflow_reference(repository, source_ref)
    identity = runner_identity(event, os.environ["JIT_PARAMETER_PREFIX"])
    instance_types = {
        "medium": os.environ["MEDIUM_INSTANCE_TYPE"],
        "large": os.environ["LARGE_INSTANCE_TYPE"],
    }
    profiles = {
        "builder": os.environ["BUILDER_PROFILE_ARN"],
        "validation": os.environ["VALIDATION_PROFILE_ARN"],
    }
    runner_size = event.get("runner_size")
    runner_profile = event.get("runner_profile")
    if runner_size not in instance_types:
        raise ValueError("invalid runner size")
    if runner_profile not in profiles:
        raise ValueError("invalid runner profile")

    live_states = {"pending", "running", "stopping", "stopped"}
    live_runners = [
        instance for instance in runner_instances() if instance["State"]["Name"] in live_states
    ]
    if len(live_runners) >= int(os.environ["MAXIMUM_CONCURRENT_RUNNERS"]):
        raise RuntimeError("ephemeral ReaverOS runner limit reached")

    token = github_token()
    ensure_workflow_access(token, repository, source_ref)
    jit = github_request(
        f"/orgs/{os.environ['GITHUB_ORGANIZATION']}/actions/runners/generate-jitconfig",
        token,
        "POST",
        {
            "labels": [
                "self-hosted",
                "Linux",
                "X64",
                "reaveros-aws",
                identity["runner_name"],
            ],
            "name": identity["runner_name"],
            "runner_group_id": int(os.environ["GITHUB_RUNNER_GROUP_ID"]),
            "work_folder": "_work",
        },
    )
    identity["runner_id"] = jit["runner"]["id"]
    try:
        ssm.put_parameter(
            Name=identity["jit_parameter"],
            Overwrite=True,
            Policies=parameter_expiration_policy(
                int(os.environ["MAXIMUM_AGE_MINUTES"]),
            ),
            Tier="Advanced",
            Type="SecureString",
            Value=jit["encoded_jit_config"],
        )
        response = ec2.run_instances(
            IamInstanceProfile={"Arn": profiles[runner_profile]},
            InstanceType=instance_types[runner_size],
            LaunchTemplate={
                "LaunchTemplateId": os.environ["LAUNCH_TEMPLATE_ID"],
                "Version": "$Latest",
            },
            MaxCount=1,
            MinCount=1,
            TagSpecifications=runner_tag_specifications(
                identity,
                repository,
                source_ref,
                runner_size,
                runner_profile,
                os.environ["RUNNER_INSTANCE_NAME"],
            ),
        )
        instance_id = response["Instances"][0]["InstanceId"]
    except (BotoCoreError, ClientError, IndexError, KeyError):
        try:
            cleanup_registration(
                identity["jit_parameter"],
                jit["runner"]["id"],
            )
        except RuntimeError as cleanup_error:
            print(f"Runner launch rollback was incomplete: {cleanup_error}")
        raise
    return {
        "instance_id": instance_id,
        "labels": ["self-hosted", "reaveros-aws", identity["runner_name"]],
        "runner_name": identity["runner_name"],
    }


def status(event):
    runner_name = require_match(
        event.get("runner_name"),
        r"reaveros-[0-9]+-[0-9]+-[a-z0-9-]{1,48}",
        "runner name",
    )
    response = github_request(
        f"/orgs/{os.environ['GITHUB_ORGANIZATION']}/actions/runners"
        f"?name={urllib.parse.quote(runner_name)}",
        github_token(),
    )
    runner = next(
        (candidate for candidate in response["runners"] if candidate["name"] == runner_name),
        None,
    )
    return {"online": runner is not None and runner["status"] == "online"}


def console_output(instance_id):
    try:
        response = ec2.get_console_output(InstanceId=instance_id, Latest=True)
        return base64.b64decode(response.get("Output", "")).decode(errors="replace")
    except (BotoCoreError, ClientError) as error:
        return f"Could not read EC2 console output: {type(error).__name__}"


def delete_jit_parameter(jit_parameter):
    try:
        ssm.delete_parameter(Name=jit_parameter)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ParameterNotFound":
            raise


def delete_github_runner(runner_id):
    try:
        github_request(
            f"/orgs/{os.environ['GITHUB_ORGANIZATION']}/actions/runners/{runner_id}",
            github_token(),
            "DELETE",
        )
    except GitHubRequestError as error:
        if error.status != 404:
            raise


def cleanup_registration(jit_parameter, runner_id):
    failures = []
    if jit_parameter is not None:
        try:
            delete_jit_parameter(jit_parameter)
        except (BotoCoreError, ClientError) as error:
            failures.append(error)
    if runner_id is not None:
        try:
            delete_github_runner(runner_id)
        except (BotoCoreError, ClientError, GitHubRequestError) as error:
            failures.append(error)
    if failures:
        summary = "; ".join(str(error) for error in failures)
        raise RuntimeError(f"runner registration cleanup failed: {summary}") from failures[0]


def terminate(event):
    instance_id = require_match(
        event.get("instance_id"),
        r"i-[0-9a-f]+",
        "instance ID",
    )
    instances = runner_instances([instance_id])
    if len(instances) != 1:
        raise ValueError("instance is not a ReaverOS runner")

    cleanup = runner_cleanup(
        instances[0],
        os.environ["JIT_PARAMETER_PREFIX"],
    )

    cleanup_error = None
    try:
        cleanup_registration(cleanup["jit_parameter"], cleanup["runner_id"])
    except RuntimeError as error:
        cleanup_error = error

    output = console_output(instance_id)
    ec2.terminate_instances(InstanceIds=[instance_id])
    if cleanup_error is not None:
        raise cleanup_error
    return {"console_output": output, "instance_id": instance_id}


def reap(_event):
    live_states = {"pending", "running", "stopping", "stopped"}
    instances = [
        instance for instance in runner_instances() if instance["State"]["Name"] in live_states
    ]
    cleanup = expired_runner_cleanup(
        instances,
        datetime.datetime.now(datetime.UTC),
        int(os.environ["MAXIMUM_AGE_MINUTES"]),
        os.environ["JIT_PARAMETER_PREFIX"],
    )
    terminated = []
    failures = []
    for runner in cleanup:
        try:
            cleanup_registration(runner["jit_parameter"], runner["runner_id"])
        except RuntimeError as error:
            failures.append((runner["instance_id"], error))
        terminated.append(runner["instance_id"])
    if terminated:
        ec2.terminate_instances(InstanceIds=terminated)
    for repository in os.environ["ALLOWED_REPOSITORIES"].split(","):
        protected_refs = []
        for instance in instances:
            if instance["InstanceId"] in terminated:
                continue
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
            if tags.get("GitHubRepository") == repository and tags.get("GitHubSourceRef"):
                protected_refs.append(tags["GitHubSourceRef"])
        try:
            prune_workflow_access(repository, protected_refs)
        except (BotoCoreError, ClientError, GitHubRequestError, RuntimeError, ValueError) as error:
            print(f"Could not prune revoked runner workflow references: {error}")
    if failures:
        summary = "; ".join(f"{instance_id}: {error}" for instance_id, error in failures)
        raise RuntimeError(f"expired runner cleanup failed: {summary}") from failures[0][1]
    return {"terminated": terminated}


def handler(event, _context):
    actions = {
        "launch": launch,
        "reap": reap,
        "status": status,
        "terminate": terminate,
    }
    action = event.get("action")
    if action not in actions:
        raise ValueError("unsupported runner control action")
    return actions[action](event)
