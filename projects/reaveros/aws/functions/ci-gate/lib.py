import base64
import hashlib
import hmac
import json
import re

approval_pattern = re.compile(r"\A/ok to test ([0-9a-f]{7,40})\Z")
repository_pattern = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def event_body(event, maximum_size=1024 * 1024):
    body = event.get("body")
    if not isinstance(body, str):
        raise ValueError("webhook body is missing")
    if event.get("isBase64Encoded", False):
        try:
            body = base64.b64decode(body, validate=True)
        except ValueError as error:
            raise ValueError("webhook body is not valid base64") from error
    else:
        body = body.encode()
    if len(body) > maximum_size:
        raise ValueError("webhook body is too large")
    return body


def event_header(event, name):
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return ""
    name = name.casefold()
    return next(
        (
            value
            for key, value in headers.items()
            if isinstance(key, str) and key.casefold() == name and isinstance(value, str)
        ),
        "",
    )


def verify_signature(body, signature, secret):
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook secret is missing")
    expected = (
        "sha256="
        + hmac.new(
            secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(signature, expected)


def parse_payload(body):
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("webhook body is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("webhook payload must be an object")
    return payload


def approval_sha(comment):
    if not isinstance(comment, str):
        return None
    match = approval_pattern.fullmatch(comment.strip())
    return match.group(1) if match else None


def starts_with_approval_command(comment):
    return isinstance(comment, str) and comment.strip().startswith("/ok to test")


def repository_identity(payload, allowed_repositories):
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("repository is missing")
    full_name = repository.get("full_name")
    repository_id = repository.get("id")
    if (
        not isinstance(full_name, str)
        or repository_pattern.fullmatch(full_name) is None
        or full_name.casefold() not in allowed_repositories
        or not isinstance(repository_id, int)
        or repository_id < 1
    ):
        raise ValueError("repository is not allowed")
    return full_name, repository_id


def installation_id(payload):
    installation = payload.get("installation")
    value = installation.get("id") if isinstance(installation, dict) else None
    if not isinstance(value, int) or value < 1:
        raise ValueError("installation is missing")
    return value


def pull_request_number(payload):
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
    else:
        issue = payload.get("issue")
        if not isinstance(issue, dict) or not isinstance(issue.get("pull_request"), dict):
            raise ValueError("event does not describe a pull request")
        number = issue.get("number")
    if not isinstance(number, int) or number < 1:
        raise ValueError("pull request number is missing")
    return number


def current_revision(pull_request):
    if (
        not isinstance(pull_request, dict)
        or pull_request.get("state") != "open"
        or pull_request.get("draft") is not False
    ):
        return None
    head = pull_request.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("pull request head SHA is invalid")
    return sha


def automatic_revision(pull_request, repository, automatic_actors):
    sha = current_revision(pull_request)
    if sha is None:
        return None
    user = pull_request.get("user")
    actor = user.get("login") if isinstance(user, dict) else None
    base = pull_request.get("base")
    base_repository = base.get("repo") if isinstance(base, dict) else None
    if (
        not isinstance(actor, str)
        or actor.casefold() not in automatic_actors
        or not isinstance(base_repository, dict)
        or not isinstance(base_repository.get("full_name"), str)
        or base_repository["full_name"].casefold() != repository.casefold()
        or base.get("ref") != base_repository.get("default_branch")
    ):
        return None
    return sha


def signed_commit_chain(commits, expected_count, expected_head, actor, bot_authors=None):
    if (
        not isinstance(expected_count, int)
        or not 1 <= expected_count <= 249
        or not isinstance(commits, list)
        or len(commits) != expected_count
        or not isinstance(actor, str)
    ):
        return False

    previous_sha = None
    for node in commits:
        commit = node.get("commit") if isinstance(node, dict) else None
        if not isinstance(commit, dict):
            return False
        sha = commit.get("oid")
        signature = commit.get("signature")
        signer = signature.get("signer") if isinstance(signature, dict) else None
        signer_login = signer.get("login") if isinstance(signer, dict) else None
        parents = commit.get("parents")
        parent_nodes = parents.get("nodes") if isinstance(parents, dict) else None
        if (
            not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or not isinstance(signature, dict)
            or signature.get("isValid") is not True
            or not isinstance(signer_login, str)
            or not isinstance(parent_nodes, list)
            or len(parent_nodes) != 1
        ):
            return False
        if signer_login.casefold() != actor.casefold() and not (
            actor.endswith("[bot]")
            and signer_login == "web-flow"
            and bot_authors is not None
            and sha in bot_authors
        ):
            return False
        parent_sha = parent_nodes[0].get("oid") if isinstance(parent_nodes[0], dict) else None
        if previous_sha is not None and parent_sha != previous_sha:
            return False
        previous_sha = sha

    return previous_sha == expected_head


def copied_branch(number):
    if not isinstance(number, int) or number < 1:
        raise ValueError("invalid pull request number")
    return f"pull-request/{number}"
