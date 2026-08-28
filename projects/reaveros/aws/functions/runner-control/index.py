import base64
import calendar
import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from lib import (
    expired_runner_cleanup,
    require_match,
    runner_cleanup,
    runner_identity,
    runner_tag_specifications,
    validate_repository,
)

ec2 = boto3.client("ec2")
ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")
cached_github_token = None
cached_github_token_expiry = 0


class GitHubRequestError(RuntimeError):
    def __init__(self, method, path, status, detail):
        super().__init__(f"GitHub {method} {path} failed: {status} {detail}")
        self.status = status


def base64_url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_app_jwt(credentials, now=None):
    if now is None:
        now = int(time.time())
    header = base64_url(json.dumps(
        {"alg": "RS256", "typ": "JWT"},
        separators=(",", ":"),
    ).encode())
    payload = base64_url(json.dumps(
        {
            "exp": now + 540,
            "iat": now - 60,
            "iss": credentials["app_id"],
        },
        separators=(",", ":"),
    ).encode())
    unsigned_token = f"{header}.{payload}".encode()
    private_key = serialization.load_pem_private_key(
        credentials["private_key"].encode(),
        password=None,
    )
    signature = private_key.sign(
        unsigned_token,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{unsigned_token.decode()}.{base64_url(signature)}"


def github_request(path, token, method="GET", body=None):
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "reaver-project-runner-controller",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise GitHubRequestError(
            method,
            path,
            error.code,
            detail,
        ) from error


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
    cached_github_token_expiry = calendar.timegm(time.strptime(
        installation_token["expires_at"],
        "%Y-%m-%dT%H:%M:%SZ",
    ))
    return cached_github_token


def parameter_expiration_policy(maximum_age_minutes, now=None):
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    expiration = now + datetime.timedelta(minutes=maximum_age_minutes + 60)
    return json.dumps(
        [{
            "Type": "Expiration",
            "Version": "1.0",
            "Attributes": {
                "Timestamp": expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }],
        separators=(",", ":"),
    )


def runner_instances(instance_ids=None):
    arguments = {
        "Filters": [{
            "Name": "tag:ReaverOSPurpose",
            "Values": ["GitHubActionsRunner"],
        }],
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


def launch(event):
    repository = event.get("repository")
    validate_repository(
        repository,
        set(os.environ["ALLOWED_REPOSITORIES"].split(",")),
    )
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
        instance
        for instance in runner_instances()
        if instance["State"]["Name"] in live_states
    ]
    if len(live_runners) >= int(os.environ["MAXIMUM_CONCURRENT_RUNNERS"]):
        raise RuntimeError("ephemeral ReaverOS runner limit reached")

    token = github_token()
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
        (
            candidate
            for candidate in response["runners"]
            if candidate["name"] == runner_name
        ),
        None,
    )
    return {"online": runner is not None and runner["status"] == "online"}


def console_output(instance_id):
    try:
        response = ec2.get_console_output(InstanceId=instance_id, Latest=True)
        return base64.b64decode(response.get("Output", "")).decode(
            errors="replace"
        )
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
        raise RuntimeError(f"runner registration cleanup failed: {summary}") \
            from failures[0]


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
    cleanup = expired_runner_cleanup(
        [
            instance
            for instance in runner_instances()
            if instance["State"]["Name"] in live_states
        ],
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
        ec2.terminate_instances(
            InstanceIds=terminated
        )
    if failures:
        summary = "; ".join(
            f"{instance_id}: {error}"
            for instance_id, error in failures
        )
        raise RuntimeError(f"expired runner cleanup failed: {summary}") \
            from failures[0][1]
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
