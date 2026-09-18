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

security_workflow = (root / ".github" / "workflows" / "security-analysis.yml").read_text(
    encoding="utf-8"
)
if "publish_results: true" in security_workflow:
    infrastructure_profile = json.loads(
        (root / "github" / "repositories" / "infrastructure.json").read_text(encoding="utf-8")
    )["profile"]
    if infrastructure_profile["visibility"] != "public":
        sys.exit("OpenSSF Scorecard results may be published only for a public repository.")
if "actions/upload-artifact@" in security_workflow:
    sys.exit("Security findings belong in code scanning, not public workflow artifacts.")

validate_workflow = (root / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
infrastructure_configuration = root / "github" / "repositories" / "infrastructure.json"
repository = json.loads(infrastructure_configuration.read_text(encoding="utf-8"))
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
if not (-1 < validator_install < aws_authentication < policy_validation < infrastructure_plan):
    sys.exit(
        "The AWS planning workflow does not install and run policy validation "
        "at the credential boundary."
    )
if "--require-hashes" not in plan_workflow:
    sys.exit("The IAM policy validator is not installed from a hash-locked dependency set.")
if "--only-binary=:all:" not in plan_workflow:
    sys.exit("The IAM policy validator installation may execute source builds.")

deploy_workflow = (root / ".github" / "workflows" / "aws-infrastructure-deploy.yml").read_text(
    encoding="utf-8"
)
if "plan_run_id" in deploy_workflow or "plan_key" not in deploy_workflow:
    sys.exit("The deployment workflow does not consume the opaque plan key.")

for deferred_consumer_operation in (
    "repositories: reaveros",
    "projects/reaveros/configure-ci",
    "actions/update-infrastructure-consumer",
):
    if deferred_consumer_operation in deploy_workflow:
        sys.exit(
            "The infrastructure deployment workflow invokes deferred ReaverOS "
            f"repository integration: {deferred_consumer_operation}"
        )

github_configuration_workflow = (
    root / ".github" / "workflows" / "github-configuration.yml"
).read_text(encoding="utf-8")
for deferred_repository_operation in (
    "repositories: reaveros",
    "github/repositories/reaveros.json",
):
    if deferred_repository_operation in github_configuration_workflow:
        sys.exit(
            "The infrastructure configuration workflow invokes deferred ReaverOS "
            f"repository integration: {deferred_repository_operation}"
        )

if "permission-actions: write" not in github_configuration_workflow:
    sys.exit("The infrastructure App token cannot converge repository OIDC policy.")

control_plane_plan_workflow = (root / ".github" / "workflows" / "aws-control-plane.yml").read_text(
    encoding="utf-8"
)
if "change_set_arn" in control_plane_plan_workflow:
    sys.exit("The public control-plane workflow exposes private change-set metadata.")
for policy_validation_path in (
    "- aws/cloudformation-parameters",
    "- aws/control-plane/**",
    "- aws/policy-validation-requirements.txt",
):
    if policy_validation_path not in control_plane_plan_workflow:
        sys.exit(
            "The control-plane workflow is not triggered by policy validation input: "
            f"{policy_validation_path}"
        )

control_plane_deploy_workflow = (
    root / ".github" / "workflows" / "aws-control-plane-deploy.yml"
).read_text(encoding="utf-8")
if "plan_key" not in control_plane_deploy_workflow:
    sys.exit("The control-plane deployment does not consume an opaque plan key.")
if "permission-actions-variables" in control_plane_deploy_workflow:
    sys.exit("The App token action does not support an actions-variables input.")
for app_workflow_name, app_workflow in (
    ("aws-control-plane-deploy.yml", control_plane_deploy_workflow),
    ("github-configuration.yml", github_configuration_workflow),
):
    if "client-id: ${{ vars.INFRASTRUCTURE_APP_CLIENT_ID }}" not in app_workflow:
        sys.exit(f"{app_workflow_name}: infrastructure App client ID is not used.")
    if "app-id:" in app_workflow:
        sys.exit(f"{app_workflow_name}: deprecated infrastructure App ID is used.")

control_validator_install = control_plane_plan_workflow.find("Install the IAM policy validator")
control_aws_authentication = control_plane_plan_workflow.find("Authenticate to AWS for planning")
control_policy_validation = control_plane_plan_workflow.find(
    "Validate IAM policies with Access Analyzer"
)
control_plane_plan = control_plane_plan_workflow.find("Create the reviewed change set")
if not (
    -1
    < control_validator_install
    < control_aws_authentication
    < control_policy_validation
    < control_plane_plan
):
    sys.exit(
        "The control-plane workflow does not validate IAM policies at its credential boundary."
    )

for workflow_name in [
    "aws-control-plane-deploy.yml",
    "aws-control-plane.yml",
    "aws-infrastructure-deploy.yml",
    "aws-infrastructure.yml",
    "github-configuration.yml",
]:
    workflow = (root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
    if "concurrency:" not in workflow or "cancel-in-progress: false" not in workflow:
        sys.exit(f"{workflow_name}: mutating workflow is not serialized")


def job_contents(workflow_name: str, job_name: str) -> str:
    lines = (
        (root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8").splitlines()
    )
    marker = f"    {job_name}:"
    try:
        start = lines.index(marker)
    except ValueError:
        sys.exit(f"{workflow_name}: missing expected job: {job_name}")
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start=start + 1)
            if line.startswith("    ") and not line.startswith("        ") and line.endswith(":")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


credentialed_jobs = {
    "aws-control-plane.yml": ["plan"],
    "aws-control-plane-deploy.yml": ["deploy", "publish"],
    "aws-infrastructure.yml": ["plan"],
    "aws-infrastructure-deploy.yml": ["deploy"],
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
