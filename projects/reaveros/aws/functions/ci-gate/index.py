import json
import logging
import os
import re

import boto3
from github_app import GitHubRequestError, create_app_jwt, request
from lib import (
    approval_sha,
    automatic_revision,
    copied_branch,
    current_revision,
    installation_id,
    pull_request_number,
    repository_identity,
    signed_pr_history,
    starts_with_approval_command,
)
from webhook import event_body, event_header, parse_payload, verify_signature

secrets = boto3.client("secretsmanager")
sqs = boto3.client("sqs")
lambda_client = boto3.client("lambda")
cached_credentials = None
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
delivery_pattern = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
active_pull_request_actions = {
    "opened",
    "ready_for_review",
    "reopened",
    "synchronize",
    "closed",
    "converted_to_draft",
}


def github_request(path, token, method="GET", body=None):
    return request(
        path,
        token,
        method,
        body,
        user_agent="reaver-project-ci-gate",
    )


def credentials():
    global cached_credentials
    if cached_credentials is None:
        response = secrets.get_secret_value(SecretId=os.environ["GITHUB_APP_SECRET_ID"])
        try:
            value = json.loads(response["SecretString"])
        except (TypeError, ValueError) as error:
            raise RuntimeError("CI gate App credentials are invalid JSON") from error
        required = ("app_id", "app_slug", "private_key", "webhook_secret")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise RuntimeError("CI gate App credentials are incomplete")
        cached_credentials = value
    return cached_credentials


def installation_token(app_credentials, event_installation_id, repository_id):
    response = github_request(
        f"/app/installations/{event_installation_id}/access_tokens",
        create_app_jwt(app_credentials),
        "POST",
        {
            "repository_ids": [repository_id],
            "permissions": {
                "contents": "write",
                "pull_requests": "write",
            },
        },
    )
    token = response.get("token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("GitHub did not issue an installation token")
    return token


def pull_request(token, repository, number):
    return github_request(f"/repos/{repository}/pulls/{number}", token)


pr_history_query = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          commit {
            oid
            signature { isValid signer { login } }
          }
        }
      }
    }
  }
}
"""


def pull_request_commits(token, repository, number, expected_count):
    if not isinstance(expected_count, int) or not 1 <= expected_count <= 249:
        return None
    owner, name = repository.split("/", 1)
    nodes = []
    cursor = None
    for _ in range(3):
        response = github_request(
            "/graphql",
            token,
            "POST",
            {
                "query": pr_history_query,
                "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
            },
        )
        if not isinstance(response, dict) or response.get("errors"):
            return None
        data = response.get("data")
        repository_data = data.get("repository") if isinstance(data, dict) else None
        pull_request_data = (
            repository_data.get("pullRequest") if isinstance(repository_data, dict) else None
        )
        commits = pull_request_data.get("commits") if isinstance(pull_request_data, dict) else None
        if not isinstance(commits, dict) or commits.get("totalCount") != expected_count:
            return None
        page_nodes = commits.get("nodes")
        page_info = commits.get("pageInfo")
        if not isinstance(page_nodes, list) or not isinstance(page_info, dict):
            return None
        nodes.extend(page_nodes)
        if len(nodes) > expected_count:
            return None
        if page_info.get("hasNextPage") is False:
            return nodes if len(nodes) == expected_count else None
        cursor = page_info.get("endCursor")
        if page_info.get("hasNextPage") is not True or not isinstance(cursor, str):
            return None
    return None


def github_signed_actor_commits(token, repository, commits, actor):
    verified = set()
    expected_type = "Bot" if actor.endswith("[bot]") else "User"
    for node in commits:
        commit = node.get("commit") if isinstance(node, dict) else None
        signature = commit.get("signature") if isinstance(commit, dict) else None
        signer = signature.get("signer") if isinstance(signature, dict) else None
        if not isinstance(signer, dict) or signer.get("login") != "web-flow":
            continue
        sha = commit.get("oid")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            continue
        response = github_request(f"/repos/{repository}/commits/{sha}", token)
        author = response.get("author") if isinstance(response, dict) else None
        details = response.get("commit") if isinstance(response, dict) else None
        verification = details.get("verification") if isinstance(details, dict) else None
        if (
            isinstance(author, dict)
            and isinstance(verification, dict)
            and author.get("type") == expected_type
            and isinstance(author.get("login"), str)
            and author["login"].casefold() == actor.casefold()
            and verification.get("verified") is True
        ):
            verified.add(sha)
    return verified


def resolve_revision(token, repository, revision):
    try:
        commit = github_request(f"/repos/{repository}/commits/{revision}", token)
    except GitHubRequestError as error:
        if error.status in {404, 422}:
            return None
        raise
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("GitHub returned an invalid commit SHA")
    return sha


def admit_copied_workflow(repository, branch):
    response = lambda_client.invoke(
        FunctionName=os.environ["RUNNER_WORKFLOW_ADMISSION_FUNCTION"],
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {
                "action": "admit_workflow",
                "repository": repository,
                "source_ref": f"refs/heads/{branch}",
            },
            separators=(",", ":"),
        ).encode(),
    )
    result = json.loads(response["Payload"].read())
    if (
        response.get("StatusCode") != 200
        or response.get("FunctionError")
        or not isinstance(result, dict)
        or result.get("admitted") is not True
    ):
        raise RuntimeError("runner App did not admit the copied workflow")


def set_copied_revision(token, repository, number, sha):
    branch = copied_branch(number)
    admit_copied_workflow(repository, branch)
    get_path = f"/repos/{repository}/git/ref/heads/{branch}"
    update_path = f"/repos/{repository}/git/refs/heads/{branch}"
    try:
        current = github_request(get_path, token)
    except GitHubRequestError as error:
        if error.status != 404:
            raise
        github_request(
            f"/repos/{repository}/git/refs",
            token,
            "POST",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )
        admit_copied_workflow(repository, branch)
        return

    current_object = current.get("object") if isinstance(current, dict) else None
    current_sha = current_object.get("sha") if isinstance(current_object, dict) else None
    if not isinstance(current_sha, str) or re.fullmatch(r"[0-9a-f]{40}", current_sha) is None:
        raise ValueError("GitHub returned an invalid copied ref")
    if current_sha != sha:
        github_request(update_path, token, "PATCH", {"sha": sha, "force": True})
    admit_copied_workflow(repository, branch)


def github_error_message(error):
    try:
        detail = json.loads(error.detail)
    except (TypeError, ValueError):
        return None
    return detail.get("message") if isinstance(detail, dict) else None


def delete_copied_revision(token, repository, number):
    try:
        github_request(
            f"/repos/{repository}/git/refs/heads/{copied_branch(number)}",
            token,
            "DELETE",
        )
    except GitHubRequestError as error:
        if error.status == 404 or (
            error.status == 422 and github_error_message(error) == "Reference does not exist"
        ):
            return
        raise


def comment(token, repository, number, message, delivery_id=None):
    if delivery_id is not None:
        marker = f"<!-- reaver-project-ci-gate-delivery:{delivery_id} -->"
        bot_login = f"{credentials()['app_slug']}[bot]"
        for page in range(1, 11):
            comments = github_request(
                f"/repos/{repository}/issues/{number}/comments?per_page=100&page={page}",
                token,
            )
            if not isinstance(comments, list):
                raise ValueError("GitHub returned invalid issue comments")
            if any(
                isinstance(entry, dict)
                and isinstance(entry.get("user"), dict)
                and entry["user"].get("login") == bot_login
                and isinstance(entry.get("body"), str)
                and entry["body"].endswith(marker)
                for entry in comments
            ):
                return
            if len(comments) < 100:
                break
        else:
            raise ValueError("Too many comments to verify CI Gate feedback")
        message = f"{message}\n\n{marker}"
    github_request(
        f"/repos/{repository}/issues/{number}/comments",
        token,
        "POST",
        {"body": message},
    )


def approver_can_run_ci(token, repository, actor):
    if not isinstance(actor, str) or not actor:
        return False
    try:
        permission = github_request(
            f"/repos/{repository}/collaborators/{actor}/permission",
            token,
        )
    except GitHubRequestError as error:
        if error.status == 404:
            return False
        raise
    return permission.get("permission") in {"admin", "maintain", "write"}


def handle_pull_request(payload, token, repository, delivery_id=None):
    action = payload.get("action")
    number = pull_request_number(payload)
    if action in {"closed", "converted_to_draft"}:
        current = pull_request(token, repository, number)
        if action == "closed" and current.get("state") != "closed":
            return "ignored stale closed event"
        if action == "converted_to_draft" and (
            current.get("state") != "open" or current.get("draft") is not True
        ):
            return "ignored stale draft event"
        delete_copied_revision(token, repository, number)
        return "removed copied revision"
    if action not in {"opened", "ready_for_review", "reopened", "synchronize"}:
        return "ignored pull request action"

    event_pull_request = payload["pull_request"]
    event_head = event_pull_request.get("head")
    event_sha = event_head.get("sha") if isinstance(event_head, dict) else None
    current = pull_request(token, repository, number)
    sha = current_revision(current)
    if sha is None:
        delete_copied_revision(token, repository, number)
        return "pull request is not eligible"
    if event_sha != sha:
        return "ignored stale pull request event"

    automatic_actors = {
        actor.strip().casefold()
        for actor in os.environ.get("AUTOMATIC_ACTORS", "").split(",")
        if actor.strip()
    }
    automatic_sha = automatic_revision(current, repository, automatic_actors)
    if automatic_sha is not None:
        actor = current["user"]["login"]
        commit_count = current.get("commits")
        commits = pull_request_commits(token, repository, number, commit_count)
        web_flow_authors = (
            github_signed_actor_commits(token, repository, commits, actor)
            if isinstance(commits, list)
            else None
        )
        if not signed_pr_history(commits, commit_count, automatic_sha, actor, web_flow_authors):
            automatic_sha = None
    if automatic_sha is None:
        delete_copied_revision(token, repository, number)
        if action in {"opened", "ready_for_review", "reopened"}:
            comment(
                token,
                repository,
                number,
                "AWS-backed CI requires a maintainer to approve this exact revision with "
                f"`/ok to test {sha[:12]}`.",
                delivery_id,
            )
        return "revision requires exact approval"

    set_copied_revision(token, repository, number, automatic_sha)
    return f"copied automatically approved revision {automatic_sha}"


def handle_issue_comment(payload, token, repository, delivery_id=None):
    if payload.get("action") != "created":
        return "ignored comment action"
    number = pull_request_number(payload)
    body = payload.get("comment", {}).get("body")
    requested_sha = approval_sha(body)
    if requested_sha is None:
        if starts_with_approval_command(body):
            current = pull_request(token, repository, number)
            sha = current_revision(current)
            expected = sha[:12] if sha is not None else "the current commit ID"
            comment(
                token,
                repository,
                number,
                "The approval must be exactly `/ok to test " + expected + "`.",
                delivery_id,
            )
        return "ignored non-approval comment"

    actor = payload.get("comment", {}).get("user", {}).get("login")
    if not approver_can_run_ci(token, repository, actor):
        comment(
            token,
            repository,
            number,
            "Only a repository maintainer can approve AWS-backed CI.",
            delivery_id,
        )
        return "commenter cannot approve CI"

    resolved_sha = resolve_revision(token, repository, requested_sha)
    current = pull_request(token, repository, number)
    sha = current_revision(current)
    if sha is None:
        return "ignored approval for ineligible pull request"
    if resolved_sha != sha:
        comment(
            token,
            repository,
            number,
            f"Refused stale CI approval for `{requested_sha}`; current revision: `{sha[:12]}`.",
            delivery_id,
        )
        return "approval does not match current revision"

    set_copied_revision(token, repository, number, resolved_sha)
    comment(token, repository, number, f"Queued AWS-backed CI for `{requested_sha}`.", delivery_id)
    return f"copied explicitly approved revision {resolved_sha}"


def response(status_code, message):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"message": message}, separators=(",", ":")),
    }


def queued_payload(event_name, payload):
    action = payload.get("action")
    if event_name == "pull_request":
        if action not in active_pull_request_actions:
            return None
        number = pull_request_number(payload)
        pull_request_data = payload["pull_request"]
        head = pull_request_data.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if action not in {"closed", "converted_to_draft"} and (
            not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None
        ):
            raise ValueError("event head SHA is invalid")
        return {
            "action": action,
            "pull_request": {"number": number, "head": {"sha": sha}},
        }

    if action != "created":
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("pull_request"), dict):
        return None
    number = pull_request_number(payload)
    comment_data = payload.get("comment")
    if not isinstance(comment_data, dict):
        raise ValueError("issue comment is missing")
    body = comment_data.get("body")
    requested_sha = approval_sha(body)
    if requested_sha is None and not starts_with_approval_command(body):
        return None
    user = comment_data.get("user")
    actor = user.get("login") if isinstance(user, dict) else None
    return {
        "action": action,
        "issue": {"number": number, "pull_request": {}},
        "comment": {
            "body": f"/ok to test {requested_sha}" if requested_sha is not None else "/ok to test",
            "user": {"login": actor},
        },
    }


def handler(event, _context):
    try:
        body = event_body(event)
        app_credentials = credentials()
        signature = event_header(event, "x-hub-signature-256")
        if not verify_signature(body, signature, app_credentials["webhook_secret"]):
            return response(401, "invalid webhook signature")
        event_name = event_header(event, "x-github-event")
        if event_name == "ping":
            return response(200, "pong")
        if event_name not in {"issue_comment", "pull_request"}:
            return response(200, "ignored webhook event")

        payload = parse_payload(body)
        allowed_repositories = {
            value.strip().casefold()
            for value in os.environ["ALLOWED_REPOSITORIES"].split(",")
            if value.strip()
        }
        repository, repository_id = repository_identity(
            payload,
            allowed_repositories,
        )
        normalized = queued_payload(event_name, payload)
        if normalized is None:
            return response(200, "ignored webhook action")
        delivery_id = event_header(event, "x-github-delivery")
        if delivery_pattern.fullmatch(delivery_id) is None:
            raise ValueError("GitHub delivery ID is invalid")
        normalized["installation"] = {"id": installation_id(payload)}
        normalized["repository"] = {"full_name": repository, "id": repository_id}
        queued = sqs.send_message(
            QueueUrl=os.environ["CI_GATE_QUEUE_URL"],
            MessageBody=json.dumps(
                {
                    "schema_version": 1,
                    "delivery_id": delivery_id,
                    "event": event_name,
                    "payload": normalized,
                },
                separators=(",", ":"),
            ),
            MessageGroupId="reaveros-ci-gate",
            MessageDeduplicationId=delivery_id,
        )
        if (
            not isinstance(queued, dict)
            or not isinstance(queued.get("MessageId"), str)
            or not queued["MessageId"]
        ):
            raise RuntimeError("SQS did not acknowledge the CI Gate event")
        logger.info(
            "queued CI Gate delivery %s for %s#%d",
            delivery_id,
            repository,
            pull_request_number(normalized),
        )
        return response(202, "queued webhook")
    except ValueError as error:
        return response(400, str(error))


def worker_handler(event, _context):
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("CI Gate worker expects exactly one queued event")
    record = records[0]
    if not isinstance(record, dict) or not isinstance(record.get("body"), str):
        raise ValueError("CI Gate queue record is invalid")
    task = json.loads(record["body"])
    if not isinstance(task, dict) or task.get("schema_version") != 1:
        raise ValueError("CI Gate queue task is invalid")
    event_name = task.get("event")
    if event_name not in {"pull_request", "issue_comment"}:
        raise ValueError("CI Gate queue event is invalid")
    delivery_id = task.get("delivery_id")
    if not isinstance(delivery_id, str) or delivery_pattern.fullmatch(delivery_id) is None:
        raise ValueError("CI Gate queue delivery ID is invalid")
    payload = task.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("CI Gate queue payload is invalid")
    allowed_repositories = {
        value.strip().casefold()
        for value in os.environ["ALLOWED_REPOSITORIES"].split(",")
        if value.strip()
    }
    repository, repository_id = repository_identity(payload, allowed_repositories)
    number = pull_request_number(payload)
    token = installation_token(credentials(), installation_id(payload), repository_id)
    if event_name == "pull_request":
        message = handle_pull_request(payload, token, repository, delivery_id)
    else:
        message = handle_issue_comment(payload, token, repository, delivery_id)
    logger.info(
        "processed CI Gate delivery %s for %s#%d: %s", delivery_id, repository, number, message
    )
    return {"number": number, "message": message}
