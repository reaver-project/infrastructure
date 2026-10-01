import re


def expected_job(job):
    runner_key = job["runner_key"]
    if runner_key == "prepare":
        names = {
            "Prepare build environment (medium) / Run AWS prepare": "medium",
            "Rebuild build environment (large) / Run AWS prepare": "large",
        }
        size = names.get(job["job_name"])
        if size is not None:
            return {"runner_size": size, "runner_profile": "builder"}
        raise ValueError("unexpected preparation job")

    tasks = {
        "build-dependencies": ("Check build-system dependencies", "amd64"),
        "unit-tests": ("Unit tests", "amd64"),
        "image": ("Build image", "uefi-efipart-amd64"),
        "boot": ("Boot smoke test", "uefi-efipart-amd64"),
    }
    for task, (name, target) in tasks.items():
        if runner_key == f"{task}-{target}":
            expected_name = f"{name} ({target}) / Run AWS {task} {target}"
            if job["job_name"] != expected_name:
                raise ValueError("unexpected validation job")
            return {"runner_size": "medium", "runner_profile": "validation"}
    raise ValueError("unexpected runner job")


def validate_fetched_job(job, fetched_job, run, trust):
    if not isinstance(fetched_job, dict) or not isinstance(run, dict):
        raise ValueError("GitHub returned invalid workflow job metadata")
    expected = expected_job(job)
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
    if fetched_job.get("workflow_name") not in {None, "CI"}:
        raise ValueError("GitHub workflow job name differs from the webhook")
    labels = fetched_job.get("labels")
    if (
        not isinstance(labels, list)
        or len(labels) != 3
        or set(labels) != {"self-hosted", "reaveros-aws", job["runner_name"]}
    ):
        raise ValueError("GitHub workflow job requests unexpected labels")
    if fetched_job.get("runner_group_id") not in {None, int(job["runner_group_id"])}:
        raise ValueError("GitHub workflow job uses an unexpected runner group")
    for name, value in (
        ("id", job["run_id"]),
        ("run_attempt", job["run_attempt"]),
        ("head_sha", job["head_sha"]),
        ("head_branch", branch),
        ("name", "CI"),
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
