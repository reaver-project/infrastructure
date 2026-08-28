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

plan_validator = validator("schemas/private-plan.schema.json")
plan = load("projects/reaveros/aws/testdata/private-plan.json")
plan_validator.validate(plan)
invalid_plan = copy.deepcopy(plan)
invalid_plan["run_id"] = 101
require_rejection(plan_validator, invalid_plan, "private plan run ID")

print("Repository JSON schema tests passed.")
