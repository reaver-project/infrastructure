import json
import re
import sys
from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]


def workflow_document(workflow_name: str) -> dict:
    path = root / ".github" / "workflows" / workflow_name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        sys.exit(f"{path}: workflow is not a mapping")
    return document


def app_token_inputs(workflow_name: str, job_name: str) -> dict:
    document = workflow_document(workflow_name)
    try:
        steps = document["jobs"][job_name]["steps"]
    except (KeyError, TypeError):
        sys.exit(f"{workflow_name}: missing expected job or steps: {job_name}")
    matches = [
        step
        for step in steps
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/create-github-app-token@")
    ]
    if len(matches) != 1:
        sys.exit(f"{workflow_name}:{job_name}: expected exactly one infrastructure App-token step")
    inputs = matches[0].get("with")
    if not isinstance(inputs, dict):
        sys.exit(f"{workflow_name}:{job_name}: App-token inputs are not a mapping")
    return inputs


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
        reference = re.match(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
        if reference is None:
            continue
        action = reference.group(1)
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

deployment_jobs = workflow_document("aws-infrastructure-deploy.yml")["jobs"]
publish_job = deployment_jobs["publish"]
consumer_job = deployment_jobs["update-consumer"]
if (
    publish_job.get("needs") != "deploy"
    or publish_job.get("environment") != "github-production"
    or set(consumer_job.get("needs", [])) != {"deploy", "publish"}
    or consumer_job.get("environment") != "github-production"
    or "projects/reaveros/configure-ci" not in deploy_workflow
    or "./actions/update-infrastructure-consumer" not in deploy_workflow
):
    sys.exit("ReaverOS contract publication must follow the reviewed AWS deployment")
publish_app_inputs = app_token_inputs("aws-infrastructure-deploy.yml", "publish")
consumer_app_inputs = app_token_inputs("aws-infrastructure-deploy.yml", "update-consumer")
if (
    publish_app_inputs.get("client-id") != "${{ vars.INFRASTRUCTURE_APP_CLIENT_ID }}"
    or publish_app_inputs.get("repositories") != "reaveros"
    or publish_app_inputs.get("owner") != "reaver-project"
    or consumer_app_inputs.get("app-id") != "${{ vars.MAINTENANCE_APP_ID }}"
    or consumer_app_inputs.get("repositories") != "reaveros"
    or consumer_app_inputs.get("owner") != "reaver-project"
):
    sys.exit("ReaverOS contract updates require separate repository-scoped App tokens")

github_configuration_workflow = (
    root / ".github" / "workflows" / "github-configuration.yml"
).read_text(encoding="utf-8")
reaveros_configuration_job = workflow_document("github-configuration.yml")["jobs"]["reaveros"]
if (
    reaveros_configuration_job.get("needs") != "deploy"
    or reaveros_configuration_job.get("environment") != "github-production"
    or "github/configure-repository github/repositories/reaveros.json"
    not in github_configuration_workflow
    or "CI_GATE_APP_ID: ${{ vars.CI_GATE_APP_ID }}" not in github_configuration_workflow
):
    sys.exit("ReaverOS repository policy is not gated on reviewed GitHub configuration")

github_configuration_app_inputs = app_token_inputs("github-configuration.yml", "deploy")
if github_configuration_app_inputs.get("permission-actions") != "write":
    sys.exit("The infrastructure App token cannot converge repository OIDC policy.")
reaveros_configuration_app_inputs = app_token_inputs("github-configuration.yml", "reaveros")
if (
    reaveros_configuration_app_inputs.get("client-id") != "${{ vars.INFRASTRUCTURE_APP_CLIENT_ID }}"
    or reaveros_configuration_app_inputs.get("owner") != "reaver-project"
    or reaveros_configuration_app_inputs.get("repositories") != "reaveros"
    or reaveros_configuration_app_inputs.get("permission-actions") != "write"
    or reaveros_configuration_app_inputs.get("permission-administration") != "write"
    or "permission-organization-administration" in reaveros_configuration_app_inputs
):
    sys.exit("ReaverOS policy must use a repository-scoped infrastructure App token")

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
control_plane_publish_app_inputs = app_token_inputs("aws-control-plane-deploy.yml", "publish")
if "permission-actions-variables" in control_plane_publish_app_inputs:
    sys.exit("The App token action does not support an actions-variables input.")
for app_workflow_name, app_inputs in (
    ("aws-control-plane-deploy.yml", control_plane_publish_app_inputs),
    ("github-configuration.yml", github_configuration_app_inputs),
):
    if app_inputs.get("client-id") != "${{ vars.INFRASTRUCTURE_APP_CLIENT_ID }}":
        sys.exit(f"{app_workflow_name}: infrastructure App client ID is not used.")
    if "app-id" in app_inputs:
        sys.exit(f"{app_workflow_name}: deprecated infrastructure App ID is used.")
    if app_inputs.get("owner") != "reaver-project" or app_inputs.get("repositories") != (
        "infrastructure"
    ):
        sys.exit(f"{app_workflow_name}: infrastructure App token is not repository-scoped.")

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
    "aws-infrastructure-deploy.yml": ["deploy", "publish", "update-consumer"],
    "github-configuration.yml": ["deploy", "reaveros"],
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
