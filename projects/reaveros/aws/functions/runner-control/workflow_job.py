import re

from runner_class import expected_runner_labels, runner_classes


def runner_specification(job):
    runner_class = job.get("runner_class")
    if runner_class is not None:
        if runner_class not in runner_classes:
            raise ValueError("unsupported runner class")
        profile, size = runner_classes[runner_class]
        return {"runner_size": size, "runner_profile": profile}

    # Jobs admitted before resource-class labels were added retain their former profiles.
    legacy_preparation = {"prepare-medium": "medium", "prepare-large": "large"}
    size = legacy_preparation.get(job["runner_key"])
    if size is not None:
        return {"runner_size": size, "runner_profile": "builder"}
    return {"runner_size": "medium", "runner_profile": "validation"}


def validate_fetched_job(job, fetched_job, run, trust):
    if not isinstance(fetched_job, dict) or not isinstance(run, dict):
        raise ValueError("GitHub returned invalid workflow job metadata")
    expected = runner_specification(job)
    branch = job["head_branch"]
    if trust == "trusted" and branch != "main":
        raise ValueError("trusted runner job has an untrusted branch")
    if trust == "candidate" and re.fullmatch(r"pull-request/[1-9][0-9]*", branch) is None:
        raise ValueError("candidate runner job has an untrusted branch")
    source_ref = f"refs/heads/{branch}"
    for name, value in (
        ("id", job["job_id"]),
        ("run_id", job["run_id"]),
        ("run_attempt", job["run_attempt"]),
        ("head_sha", job["head_sha"]),
        ("head_branch", branch),
        ("name", job["job_name"]),
    ):
        if fetched_job.get(name) != value:
            raise ValueError(f"GitHub workflow job {name} differs from the webhook")
    runner_class = job.get("runner_class")
    expected_labels = expected_runner_labels(job["runner_name"], runner_class)
    labels = fetched_job.get("labels")
    if (
        not isinstance(labels, list)
        or len(labels) != len(expected_labels)
        or set(labels) != expected_labels
    ):
        raise ValueError("GitHub workflow job requests unexpected labels")
    if fetched_job.get("runner_group_id") not in {None, int(job["runner_group_id"])}:
        raise ValueError("GitHub workflow job uses an unexpected runner group")
    for name, value in (
        ("id", job["run_id"]),
        ("run_attempt", job["run_attempt"]),
        ("head_sha", job["head_sha"]),
        ("head_branch", branch),
        ("path", ".github/workflows/ci.yml"),
    ):
        if run.get(name) != value:
            raise ValueError(f"GitHub workflow run {name} differs from the webhook")
    repository = run.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("full_name", "").casefold() != job["repository"].casefold()
        or repository.get("id") != job["repository_id"]
    ):
        raise ValueError("GitHub workflow run repository differs from the webhook")
    if run.get("event") not in {"push", "schedule", "workflow_dispatch"}:
        raise ValueError("GitHub workflow run has an unexpected trigger")
    return {**expected, "source_ref": source_ref}
