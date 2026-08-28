import json
import os
import re

import boto3
from github_app import GitHubRequestError, create_app_jwt, request
from lib import (
    approval_sha,
    automatic_revision,
    copied_branch,
    current_revision,
    event_body,
    event_header,
    installation_id,
    parse_payload,
    pull_request_number,
    repository_identity,
    starts_with_approval_command,
    verify_signature,
)

secrets = boto3.client("secretsmanager")
cached_credentials = None


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
        value = json.loads(response["SecretString"])
        required = ("app_id", "app_slug", "private_key", "webhook_secret")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise ValueError("CI gate App credentials are incomplete")
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
                "issues": "write",
                "pull_requests": "read",
            },
        },
    )
    token = response.get("token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("GitHub did not issue an installation token")
    return token


def pull_request(token, repository, number):
    return github_request(f"/repos/{repository}/pulls/{number}", token)


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


def set_copied_revision(token, repository, number, sha):
    branch = copied_branch(number)
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
        return

    current_object = current.get("object") if isinstance(current, dict) else None
    current_sha = current_object.get("sha") if isinstance(current_object, dict) else None
    if not isinstance(current_sha, str) or re.fullmatch(r"[0-9a-f]{40}", current_sha) is None:
        raise ValueError("GitHub returned an invalid copied ref")
    if current_sha != sha:
        github_request(update_path, token, "PATCH", {"sha": sha, "force": True})


def delete_copied_revision(token, repository, number):
    try:
        github_request(
            f"/repos/{repository}/git/refs/heads/{copied_branch(number)}",
            token,
            "DELETE",
        )
    except GitHubRequestError as error:
        if error.status != 404:
            raise


def comment(token, repository, number, message):
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
    return permission.get("permission") in {"admin", "write"}


def handle_pull_request(payload, token, repository):
    action = payload.get("action")
    number = pull_request_number(payload)
    if action in {"closed", "converted_to_draft"}:
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
    if automatic_sha is None:
        delete_copied_revision(token, repository, number)
        if action in {"opened", "ready_for_review", "reopened"}:
            comment(
                token,
                repository,
                number,
                "AWS-backed CI requires a maintainer to approve this exact revision with "
                f"`/ok to test {sha[:12]}`.",
            )
        return "revision requires exact approval"

    set_copied_revision(token, repository, number, automatic_sha)
    return f"copied automatically approved revision {automatic_sha}"


def handle_issue_comment(payload, token, repository):
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
            )
        return "ignored non-approval comment"

    actor = payload.get("comment", {}).get("user", {}).get("login")
    if not approver_can_run_ci(token, repository, actor):
        comment(
            token,
            repository,
            number,
            "Only a repository maintainer can approve AWS-backed CI.",
        )
        return "commenter cannot approve CI"

    resolved_sha = resolve_revision(token, repository, requested_sha)
    current = pull_request(token, repository, number)
    sha = current_revision(current)
    if sha is None or resolved_sha != sha:
        expected = sha[:12] if sha is not None else "no eligible revision"
        comment(
            token,
            repository,
            number,
            f"Refused stale CI approval for `{requested_sha}`; current revision: `{expected}`.",
        )
        return "approval does not match current revision"

    set_copied_revision(token, repository, number, resolved_sha)
    comment(token, repository, number, f"Queued AWS-backed CI for `{requested_sha}`.")
    return f"copied explicitly approved revision {resolved_sha}"


def response(status_code, message):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"message": message}, separators=(",", ":")),
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
        token = installation_token(
            app_credentials,
            installation_id(payload),
            repository_id,
        )
        if event_name == "pull_request":
            message = handle_pull_request(payload, token, repository)
        else:
            message = handle_issue_comment(payload, token, repository)
        return response(200, message)
    except ValueError as error:
        return response(400, str(error))
