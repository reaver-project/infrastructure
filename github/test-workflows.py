import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
yaml_files = [
    *sorted((root / ".github" / "workflows").glob("*.yml")),
    *sorted((root / ".github" / "actions").glob("*/action.yml")),
    *sorted((root / "actions").glob("*/action.yml")),
]
if not yaml_files:
    sys.exit("No GitHub Actions workflows found.")

for yaml_file in yaml_files:
    contents = yaml_file.read_text(encoding="utf-8")
    for line_number, line in enumerate(contents.splitlines(), start=1):
        if "uses:" not in line:
            continue
        action = line.split("uses:", 1)[1].split("#", 1)[0].strip()
        if action.startswith("./"):
            continue
        if "@" not in action or len(action.rsplit("@", 1)[1]) != 40:
            sys.exit(f"{yaml_file}:{line_number}: action is not pinned by SHA: {action}")

all_workflows = "\n".join(
    workflow.read_text(encoding="utf-8")
    for workflow in sorted((root / ".github" / "workflows").glob("*.yml"))
)

security_workflow = (
    root / ".github" / "workflows" / "security-analysis.yml"
).read_text(encoding="utf-8")
if "publish_results: true" in security_workflow:
    infrastructure_profile = json.loads(
        (root / "github" / "repositories" / "infrastructure.json").read_text(
            encoding="utf-8"
        )
    )["profile"]
    if infrastructure_profile["visibility"] != "public":
        sys.exit("OpenSSF Scorecard results may be published only for a public repository.")
if "actions/upload-artifact@" in security_workflow:
    sys.exit("Security findings belong in code scanning, not public workflow artifacts.")

validate_workflow = (
    root / ".github" / "workflows" / "validate.yml"
).read_text(encoding="utf-8")
infrastructure_configuration = (
    root / "github" / "repositories" / "infrastructure.json"
)
repository = json.loads(
    infrastructure_configuration.read_text(encoding="utf-8")
)
for ruleset in repository["rulesets"]:
    for rule in ruleset["rules"]:
        if rule["type"] != "required_status_checks":
            continue
        for status_check in rule["parameters"]["required_status_checks"]:
            context = status_check["context"]
            if f"name: {context}" not in validate_workflow:
                sys.exit(
                    f"{infrastructure_configuration}: required check is not emitted "
                    f"by validate.yml: {context}"
                )

for public_artifact_action in ("actions/upload-artifact@", "actions/download-artifact@"):
    if public_artifact_action in all_workflows:
        sys.exit(f"AWS deployment plans must not use {public_artifact_action}")

plan_workflow = (root / ".github" / "workflows" / "aws-infrastructure.yml").read_text(
    encoding="utf-8"
)
if "change_set_arn" in plan_workflow or "change_set_type" in plan_workflow:
    sys.exit("The public planning workflow exposes private change-set metadata.")

validator_install = plan_workflow.find("Install the IAM policy validator")
aws_authentication = plan_workflow.find("Authenticate to AWS for planning")
policy_validation = plan_workflow.find("Validate IAM policies with Access Analyzer")
infrastructure_plan = plan_workflow.find("Create the reviewed change set")
if not (
    -1 < validator_install < aws_authentication < policy_validation < infrastructure_plan
):
    sys.exit(
        "The AWS planning workflow does not install and run policy validation "
        "at the credential boundary."
    )
if "--require-hashes" not in plan_workflow:
    sys.exit("The IAM policy validator is not installed from a hash-locked dependency set.")

deploy_workflow = (
    root / ".github" / "workflows" / "aws-infrastructure-deploy.yml"
).read_text(encoding="utf-8")
if "plan_run_id" in deploy_workflow or "plan_key" not in deploy_workflow:
    sys.exit("The deployment workflow does not consume the opaque plan key.")

for workflow_name in [
    "aws-infrastructure-deploy.yml",
    "aws-infrastructure.yml",
    "github-configuration.yml",
]:
    workflow = (root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
    if "concurrency:" not in workflow or "cancel-in-progress: false" not in workflow:
        sys.exit(f"{workflow_name}: mutating workflow is not serialized")


def job_contents(workflow_name: str, job_name: str) -> str:
    lines = (
        root / ".github" / "workflows" / workflow_name
    ).read_text(encoding="utf-8").splitlines()
    marker = f"    {job_name}:"
    try:
        start = lines.index(marker)
    except ValueError:
        sys.exit(f"{workflow_name}: missing expected job: {job_name}")
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start=start + 1)
            if line.startswith("    ")
            and not line.startswith("        ")
            and line.endswith(":")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


credentialed_jobs = {
    "aws-infrastructure.yml": ["plan"],
    "aws-infrastructure-deploy.yml": ["deploy", "configure-reaveros"],
    "github-configuration.yml": ["deploy"],
    "security-analysis.yml": ["actions_security", "scorecard"],
}
for workflow_name, job_names in credentialed_jobs.items():
    for job_name in job_names:
        job = job_contents(workflow_name, job_name)
        first_step = job.find("          - name:")
        hardening = job.find("          - name: Harden the runner")
        if first_step != hardening or "egress-policy: audit" not in job:
            sys.exit(
                f"{workflow_name}:{job_name}: credentialed job is not hardened "
                "in audit mode before any other step"
            )

print("GitHub Actions workflow tests passed.")
