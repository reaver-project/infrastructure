import base64
import binascii
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
from workflow_job import validate_fetched_job

ec2 = boto3.client("ec2")
sqs = boto3.client("sqs")
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


def configure_webhook(_event):
    if os.environ.get("CACHE_TRUST_CLASS") != "trusted":
        raise ValueError("only the trusted controller can configure the runner App webhook")
    credentials = json.loads(
        secrets.get_secret_value(SecretId=os.environ["GITHUB_APP_SECRET_ID"])["SecretString"]
    )
    webhook_secret = secrets.get_secret_value(SecretId=os.environ["RUNNER_WEBHOOK_SECRET_ID"])[
        "SecretString"
    ]
    webhook_url = os.environ["RUNNER_WEBHOOK_URL"]
    if not isinstance(webhook_secret, str) or len(webhook_secret) < 32:
        raise ValueError("runner webhook secret is invalid")
    parsed_url = urllib.parse.urlparse(webhook_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or not parsed_url.hostname.endswith(f".lambda-url.{os.environ['AWS_REGION']}.on.aws")
        or parsed_url.path != "/"
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("runner webhook URL is invalid")
    config = github_request(
        "/app/hook/config",
        create_app_jwt(credentials),
        "PATCH",
        {"url": webhook_url, "content_type": "json", "secret": webhook_secret},
    )
    if not isinstance(config, dict) or config.get("url") != webhook_url:
        raise RuntimeError("GitHub did not retain the runner webhook URL")
    return {"configured": True}


def admit_workflow(event):
    if os.environ.get("CACHE_TRUST_CLASS") != "candidate":
        raise ValueError("only the candidate controller can admit copied workflows")
    repository = event.get("repository")
    validate_repository(repository, set(os.environ["ALLOWED_REPOSITORIES"].split(",")))
    source_ref = event.get("source_ref")
    required = workflow_reference(repository, source_ref)
    if not source_ref.startswith("refs/heads/pull-request/"):
        raise ValueError("only copied pull-request workflows can be admitted")
    token = github_token()
    path, selected = restricted_workflows(token, repository)
    if required not in selected:
        set_restricted_workflows(path, token, selected | {required})
    return {"admitted": True}


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


def instance_cache_trust(instance):
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    # Instances launched before the cache split had no trust tag.
    return tags.get("ReaverProjectCacheTrust", "candidate")


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
        or group.get("visibility") != "selected"
        or group.get("allows_public_repositories") is not True
    ):
        raise ValueError("runner group is not restricted to ReaverOS workflows")
    prefix = f"{repository}/.github/workflows/aws-runner.yml@"
    for reference in selected:
        if not reference.startswith(prefix):
            raise ValueError("runner group contains an unexpected workflow")
        workflow_reference(repository, reference.removeprefix(prefix))

    return path, set(selected)


def set_restricted_workflows(path, token, requested):
    result = github_request(
        path,
        token,
        "PATCH",
        {"selected_workflows": sorted(requested)},
    )
    retained = result.get("selected_workflows") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("name") != "reaveros"
        or result.get("visibility") != "selected"
        or result.get("allows_public_repositories") is not True
        or result.get("restricted_to_workflows") is not True
        or not isinstance(retained, list)
        or set(retained) != requested
    ):
        raise RuntimeError("runner group did not retain restricted workflow access")


def copied_workflow_references(repository, protected_refs=()):
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
        if not isinstance(source_ref, str):
            raise ValueError("GitHub returned an invalid copied CI ref")
        try:
            active.add(workflow_reference(repository, source_ref))
        except ValueError:
            continue
    return active


def live_workflow_refs(instances, repository):
    protected_refs = []
    for instance in instances:
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        source_ref = tags.get("GitHubSourceRef")
        if tags.get("GitHubRepository") != repository or not isinstance(source_ref, str):
            continue
        try:
            workflow_reference(repository, source_ref)
        except ValueError:
            continue
        protected_refs.append(source_ref)
    return protected_refs


def ensure_workflow_access(token, repository, source_ref, protected_refs=()):
    path, selected = restricted_workflows(token, repository)
    required = {
        workflow_reference(repository, "refs/heads/main"),
        workflow_reference(repository, source_ref),
    }
    if required <= selected:
        return
    active = copied_workflow_references(repository, protected_refs)
    requested = (selected & active) | required
    if requested != selected:
        set_restricted_workflows(path, token, requested)


def prune_workflow_access(repository, protected_refs=()):
    active = copied_workflow_references(repository, protected_refs)

    token = github_token()
    path, selected = restricted_workflows(token, repository)
    requested = selected & active
    requested.add(workflow_reference(repository, "refs/heads/main"))
    if requested != selected:
        set_restricted_workflows(path, token, requested)


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
    cache_trust = os.environ["CACHE_TRUST_CLASS"]
    if runner_size not in instance_types:
        raise ValueError("invalid runner size")
    if runner_profile not in profiles:
        raise ValueError("invalid runner profile")
    if cache_trust not in {"candidate", "trusted"}:
        raise ValueError("invalid cache trust class")

    live_states = {"pending", "running", "stopping", "stopped"}
    live_runners = [
        instance for instance in runner_instances() if instance["State"]["Name"] in live_states
    ]
    if "job_id" in identity:
        matching = []
        for instance in live_runners:
            tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
            if tags.get("GitHubJobId") == identity["job_id"]:
                matching.append((instance, tags))
        if len(matching) > 1:
            raise RuntimeError("multiple runners exist for one workflow job")
        if matching:
            instance, tags = matching[0]
            expected = {
                "GitHubRepository": repository,
                "GitHubSourceRef": source_ref,
                "GitHubRunId": identity["run_id"],
                "GitHubRunnerName": identity["runner_name"],
                "ReaverOSRunnerProfile": runner_profile,
                "ReaverOSRunnerSize": runner_size,
                "ReaverProjectCacheTrust": cache_trust,
            }
            if any(tags.get(key) != value for key, value in expected.items()):
                raise ValueError("existing workflow job runner differs from the request")
            if instance["State"]["Name"] not in {"pending", "running"}:
                raise RuntimeError("existing workflow job runner is shutting down")
            return {
                "instance_id": instance["InstanceId"],
                "labels": ["self-hosted", "reaveros-aws", identity["runner_name"]],
                "runner_name": identity["runner_name"],
            }
    # The candidate and trusted functions can each admit one runner at once.
    # Reserve the other in-flight launch before checking the global limit.
    admission_limit = (
        int(os.environ["MAXIMUM_CONCURRENT_RUNNERS"])
        - int(os.environ["MAXIMUM_PARALLEL_CONTROLLER_LAUNCHES"])
        + 1
    )
    if admission_limit < 1 or len(live_runners) >= admission_limit:
        raise RuntimeError("ephemeral ReaverOS runner limit reached")

    token = github_token()
    ensure_workflow_access(
        token,
        repository,
        source_ref,
        live_workflow_refs(live_runners, repository),
    )
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
                cache_trust,
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
        output = response.get("Output") or ""
        try:
            return base64.b64decode(output, validate=True).decode(errors="replace")
        except (ValueError, binascii.Error):
            # EC2 sometimes returns decoded console text despite documenting base64.
            return output
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
    if instance_cache_trust(instances[0]) != os.environ["CACHE_TRUST_CLASS"]:
        raise ValueError("runner belongs to another cache trust class")

    cleanup = runner_cleanup(
        instances[0],
        os.environ["JIT_PARAMETER_PREFIX"],
    )

    cleanup_error = None
    try:
        cleanup_registration(cleanup["jit_parameter"], cleanup["runner_id"])
    except RuntimeError as error:
        cleanup_error = error

    try:
        output = console_output(instance_id)
    finally:
        ec2.terminate_instances(InstanceIds=[instance_id])
    if cleanup_error is not None:
        raise cleanup_error
    return {"console_output": output, "instance_id": instance_id}


def reap(_event):
    live_states = {"pending", "running", "stopping", "stopped"}
    instances = [
        instance for instance in runner_instances() if instance["State"]["Name"] in live_states
    ]
    owned_instances = [
        instance
        for instance in instances
        if instance_cache_trust(instance) == os.environ["CACHE_TRUST_CLASS"]
    ]
    cleanup = expired_runner_cleanup(
        owned_instances,
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
    for instance in owned_instances:
        if instance["InstanceId"] in terminated or instance["State"]["Name"] not in {
            "pending",
            "running",
        }:
            continue
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        job_id = tags.get("GitHubJobId")
        repository = tags.get("GitHubRepository")
        if job_id is None:
            continue
        try:
            require_match(job_id, r"[1-9][0-9]*", "job ID")
            validate_repository(repository, set(os.environ["ALLOWED_REPOSITORIES"].split(",")))
            job = github_request(f"/repos/{repository}/actions/jobs/{job_id}", github_token())
            if job.get("status") == "completed":
                terminate({"instance_id": instance["InstanceId"]})
                terminated.append(instance["InstanceId"])
        except (
            BotoCoreError,
            ClientError,
            GitHubRequestError,
            RuntimeError,
            ValueError,
        ) as error:
            print(f"Could not reconcile runner job {job_id}: {error}")
    if os.environ["CACHE_TRUST_CLASS"] == "candidate":
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
            except (
                BotoCoreError,
                ClientError,
                GitHubRequestError,
                RuntimeError,
                ValueError,
            ) as error:
                print(f"Could not prune revoked runner workflow references: {error}")
    if failures:
        summary = "; ".join(f"{instance_id}: {error}" for instance_id, error in failures)
        raise RuntimeError(f"expired runner cleanup failed: {summary}") from failures[0][1]
    return {"terminated": terminated}


def workflow_job(event):
    if event.get("schema_version") != 1:
        raise ValueError("unsupported runner webhook schema")
    job = event.get("job")
    if not isinstance(job, dict):
        raise ValueError("runner webhook job is missing")
    repository = job.get("repository")
    validate_repository(repository, set(os.environ["ALLOWED_REPOSITORIES"].split(",")))
    for name in ("job_id", "run_id", "run_attempt", "repository_id", "installation_id"):
        if type(job.get(name)) is not int or job[name] < 1:
            raise ValueError(f"invalid runner webhook {name}")
    action = job.get("action")
    if action not in {"queued", "completed"}:
        raise ValueError("invalid runner webhook action")
    job["runner_group_id"] = int(os.environ["GITHUB_RUNNER_GROUP_ID"])

    token = github_token()
    fetched_job = github_request(f"/repos/{repository}/actions/jobs/{job['job_id']}", token)
    run = github_request(f"/repos/{repository}/actions/runs/{job['run_id']}", token)
    specification = validate_fetched_job(job, fetched_job, run, os.environ["CACHE_TRUST_CLASS"])
    if action == "queued":
        if fetched_job.get("status") != "queued" or run.get("status") == "completed":
            return {"ignored": "workflow job is no longer queued"}
        return launch(
            {
                "repository": repository,
                "source_ref": specification["source_ref"],
                "github_run_id": job["run_id"],
                "github_run_attempt": job["run_attempt"],
                "github_job_id": job["job_id"],
                "runner_key": job["runner_key"],
                "runner_size": specification["runner_size"],
                "runner_profile": specification["runner_profile"],
            }
        )
    if fetched_job.get("status") != "completed":
        raise RuntimeError("completed workflow job is not yet visible from GitHub")
    live_states = {"pending", "running", "stopping", "stopped"}
    matches = []
    for instance in runner_instances():
        if instance["State"]["Name"] not in live_states:
            continue
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        if tags.get("GitHubJobId") != str(job["job_id"]):
            continue
        if (
            tags.get("GitHubRepository") != repository
            or tags.get("GitHubRunId") != str(job["run_id"])
            or tags.get("GitHubSourceRef") != specification["source_ref"]
            or tags.get("GitHubRunnerName") != job["runner_name"]
            or tags.get("ReaverProjectCacheTrust") != os.environ["CACHE_TRUST_CLASS"]
        ):
            raise ValueError("workflow job runner tags differ from the webhook")
        matches.append(instance)
    if len(matches) > 1:
        raise RuntimeError("multiple runners exist for one workflow job")
    if not matches:
        return {"ignored": "workflow job has no active runner"}
    return terminate({"instance_id": matches[0]["InstanceId"]})


def handler(event, _context):
    if "Records" in event:
        records = event["Records"]
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("runner webhook batch must contain one record")
        record = records[0]
        if not isinstance(record, dict) or record.get("eventSource") != "aws:sqs":
            raise ValueError("runner webhook record is not from SQS")
        delivery = json.loads(record["body"])
        return workflow_job(delivery)
    actions = {
        "admit_workflow": admit_workflow,
        "configure_webhook": configure_webhook,
        "launch": launch,
        "reap": reap,
        "status": status,
        "terminate": terminate,
    }
    action = event.get("action")
    if action not in actions:
        raise ValueError("unsupported runner control action")
    return actions[action](event)


def sqs_handler(event, context):
    records = event.get("Records")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("runner webhook batch must contain one record")
    try:
        handler(event, context)
    except RuntimeError as error:
        if str(error) != "ephemeral ReaverOS runner limit reached":
            raise
        record = records[0]
        sqs.change_message_visibility(
            QueueUrl=os.environ["RUNNER_QUEUE_URL"],
            ReceiptHandle=record["receiptHandle"],
            VisibilityTimeout=30,
        )
        return {"batchItemFailures": [{"itemIdentifier": record["messageId"]}]}
    return {"batchItemFailures": []}


def admission_handler(event, _context):
    if not isinstance(event, dict) or event.get("action") != "admit_workflow":
        raise ValueError("unsupported runner workflow admission action")
    return admit_workflow(event)
