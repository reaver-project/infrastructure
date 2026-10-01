import re


def positive_integer(value, description):
    if type(value) is not int or value < 1:
        raise ValueError(f"invalid {description}")
    return value


def normalize_job_event(payload, allowed_repositories):
    action = payload.get("action")
    if action not in {"queued", "completed"}:
        return None

    repository = payload.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("repository is missing")
    repository_name = repository.get("full_name")
    if (
        not isinstance(repository_name, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_name) is None
    ):
        raise ValueError("repository name is invalid")
    if repository_name.casefold() not in allowed_repositories:
        return None
    repository_id = positive_integer(repository.get("id"), "repository ID")

    job = payload.get("workflow_job")
    if not isinstance(job, dict):
        raise ValueError("workflow job is missing")
    labels = job.get("labels")
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise ValueError("workflow job labels are invalid")
    if "reaveros-aws" not in labels:
        return None

    job_id = positive_integer(job.get("id"), "job ID")
    run_id = positive_integer(job.get("run_id"), "run ID")
    run_attempt = positive_integer(job.get("run_attempt"), "run attempt")
    branch = job.get("head_branch")
    if branch == "main":
        trust = "trusted"
    elif isinstance(branch, str) and re.fullmatch(r"pull-request/[1-9][0-9]*", branch):
        trust = "candidate"
    else:
        raise ValueError("workflow job branch is not admitted")

    head_sha = job.get("head_sha")
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise ValueError("workflow job SHA is invalid")
    job_name = job.get("name")
    if not isinstance(job_name, str) or not 1 <= len(job_name) <= 256:
        raise ValueError("workflow job name is invalid")
    accepted_statuses = {"queued", "waiting"} if action == "queued" else {"completed"}
    if job.get("status") not in accepted_statuses:
        raise ValueError("workflow job status does not match webhook action")

    runner_prefix = f"reaveros-{run_id}-{run_attempt}-"
    runner_names = [label for label in labels if label.startswith(runner_prefix)]
    if len(runner_names) != 1:
        raise ValueError("workflow job needs exactly one runner identity")
    runner_name = runner_names[0]
    runner_key = runner_name.removeprefix(runner_prefix)
    if (
        len(runner_name) > 64
        or re.fullmatch(r"[a-z0-9-]{1,48}", runner_key) is None
        or len(labels) != 3
        or set(labels) != {"self-hosted", "reaveros-aws", runner_name}
    ):
        raise ValueError("workflow job requests unexpected runner labels")

    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise ValueError("runner App installation is missing")
    installation_id = positive_integer(installation.get("id"), "installation ID")

    return trust, {
        "schema_version": 1,
        "action": action,
        "repository": repository_name,
        "repository_id": repository_id,
        "installation_id": installation_id,
        "job_id": job_id,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "head_branch": branch,
        "head_sha": head_sha,
        "job_name": job_name,
        "runner_key": runner_key,
        "runner_name": runner_name,
    }
