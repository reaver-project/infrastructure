#!/usr/bin/env python3

import copy
import json
import pathlib

import jsonschema

root = pathlib.Path(__file__).resolve().parent.parent


def load(path):
    return json.loads((root / path).read_text(encoding="utf-8"))


def validator(schema_path):
    schema = load(schema_path)
    validator_type = jsonschema.validators.validator_for(schema)
    validator_type.check_schema(schema)
    return validator_type(schema, format_checker=jsonschema.FormatChecker())


def require_rejection(schema_validator, document, description):
    try:
        schema_validator.validate(document)
    except jsonschema.ValidationError:
        return
    raise SystemExit(f"Schema accepted invalid {description}.")


aws_organization_validator = validator("schemas/aws-organization.schema.json")
aws_organization = load("aws/organization/reaver-project.json")
aws_organization_validator.validate(aws_organization)
invalid_aws_organization = copy.deepcopy(aws_organization)
invalid_aws_organization["control_tower"]["version"] = "latest"
require_rejection(
    aws_organization_validator,
    invalid_aws_organization,
    "Control Tower version",
)

aws_cost_controls_validator = validator("schemas/aws-cost-controls.schema.json")
aws_cost_controls = load("aws/organization/cost-controls.json")
aws_cost_controls_validator.validate(aws_cost_controls)
invalid_aws_cost_controls = copy.deepcopy(aws_cost_controls)
invalid_aws_cost_controls["budgets"][0]["amount"] = 0
require_rejection(
    aws_cost_controls_validator,
    invalid_aws_cost_controls,
    "AWS budget limit",
)


organization_validator = validator("schemas/github-organization.schema.json")
organization = load("github/organizations/reaver-project.json")
organization_validator.validate(organization)
invalid_organization = copy.deepcopy(organization)
invalid_organization["actions"]["unknown_setting"] = True
require_rejection(organization_validator, invalid_organization, "organization setting")
invalid_code_security = copy.deepcopy(organization)
invalid_code_security["code_security"]["configuration"]["secret_scanning"] = "sometimes"
require_rejection(
    organization_validator,
    invalid_code_security,
    "code security feature status",
)

repository_validator = validator("schemas/github-repository.schema.json")
for repository_path in (
    "github/repositories/infrastructure.json",
    "github/repositories/reaveros.json",
):
    repository_validator.validate(load(repository_path))
invalid_repository = load("github/repositories/infrastructure.json")
invalid_repository["rulesets"][0]["rules"][-1]["parameters"]["required_status_checks"][0].pop(
    "context"
)
require_rejection(repository_validator, invalid_repository, "required status check")

manifest_validator = validator("schemas/github-app-manifest.schema.json")
for manifest_path in (
    "github/apps/ci-gate/manifest.json",
    "github/apps/infrastructure/manifest.json",
    "github/apps/maintenance/manifest.json",
    "github/apps/runner/manifest.json",
):
    manifest_validator.validate(load(manifest_path))

maintenance_manifest = load("github/apps/maintenance/manifest.json")
if maintenance_manifest["default_permissions"] != {
    "checks": "read",
    "contents": "write",
    "pull_requests": "write",
    "statuses": "read",
    "workflows": "write",
}:
    raise SystemExit("Maintenance App permissions exceed its maintenance PR workflow.")
if maintenance_manifest["default_events"]:
    raise SystemExit("Maintenance App must not subscribe to webhook events.")

invalid_manifest = load("github/apps/runner/manifest.json")
invalid_manifest["public"] = True
require_rejection(manifest_validator, invalid_manifest, "public infrastructure App")
invalid_manifest = load("github/apps/runner/manifest.json")
invalid_manifest["hook_attributes"] = {"active": False}
require_rejection(manifest_validator, invalid_manifest, "webhook without URL")

plan_validator = validator("schemas/private-plan.schema.json")
plan = load("projects/reaveros/aws/testdata/private-plan.json")
plan_validator.validate(plan)
invalid_plan = copy.deepcopy(plan)
invalid_plan["run_id"] = 101
require_rejection(plan_validator, invalid_plan, "private plan run ID")

control_plane_plan_validator = validator("schemas/control-plane-plan.schema.json")
control_plane_plan = load("aws/control-plane/testdata/private-plan.valid.json")
control_plane_plan_validator.validate(control_plane_plan)
invalid_control_plane_plan = copy.deepcopy(control_plane_plan)
invalid_control_plane_plan["target"] = "reaveros"
require_rejection(
    control_plane_plan_validator,
    invalid_control_plane_plan,
    "control-plane plan target",
)

print("Repository JSON schema tests passed.")
