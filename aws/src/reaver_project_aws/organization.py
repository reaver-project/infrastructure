import argparse
import json
import sys
import time
from pathlib import Path

from . import plan_contract
from .api import AwsApi

ROOT_AUDIT_POLICY_ARN = "arn:aws:iam::aws:policy/root-task/IAMAuditRootUserCredentials"


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def exactly_one(items, description):
    if len(items) != 1:
        fail(f"Expected exactly one {description}; found {len(items)}.")
    return items[0]


def index_unique(items, key, description):
    indexed = {}
    for item in items:
        value = item[key]
        if value in indexed:
            fail(f"Duplicate {description}: {value}.")
        indexed[value] = item
    return indexed


def credential_state(summary):
    return {
        "password": summary.get("AccountPasswordPresent", 0),
        "access_keys": summary.get("AccountAccessKeysPresent", 0),
        "signing_certificates": summary.get("AccountSigningCertificatesPresent", 0),
        "mfa": summary.get("AccountMFAEnabled", 0),
    }


def inspect_accounts(config, api, organizations, root):
    ous = index_unique(
        api.collect(
            "organizations",
            "list_organizational_units_for_parent",
            "OrganizationalUnits",
            ParentId=root["Id"],
        ),
        "Name",
        "organizational unit name",
    )
    expected_ous = set(config["organizational_units"])
    if set(ous) != expected_ous:
        fail(f"Organization OU mismatch: expected {sorted(expected_ous)}, found {sorted(ous)}.")

    accounts = index_unique(
        api.collect("organizations", "list_accounts", "Accounts"),
        "Name",
        "account name",
    )
    expected_accounts = index_unique(
        config["accounts"],
        "name",
        "configured account name",
    )
    for expected in expected_accounts.values():
        account = accounts.get(expected["name"])
        if (
            account is None
            or account["Status"] != "ACTIVE"
            or account["Id"] != expected["account_id"]
        ):
            fail(f"Required active account differs from desired state: {expected['name']}.")
        parent = exactly_one(
            organizations.list_parents(ChildId=account["Id"])["Parents"],
            f"parent for {expected['name']}",
        )
        expected_parent = ous[expected["organizational_unit"]]["Id"]
        if parent["Id"] != expected_parent:
            fail(f"Account {expected['name']} is not in {expected['organizational_unit']}.")
    return ous, expected_accounts


def inspect_root_credentials(config, api, expected_accounts):
    iam = api.client("iam")
    management_state = credential_state(iam.get_account_summary()["SummaryMap"])
    if management_state != config["root_credentials"]["management"]:
        fail("Management root credential state differs from desired state.")

    sts = api.client("sts")
    for account in expected_accounts.values():
        credentials = sts.assume_root(
            DurationSeconds=300,
            TargetPrincipal=account["account_id"],
            TaskPolicyArn={"arn": ROOT_AUDIT_POLICY_ARN},
        )["Credentials"]
        member_state = credential_state(
            api.root_iam(credentials).get_account_summary()["SummaryMap"]
        )
        if member_state != config["root_credentials"]["members"]:
            fail(f"Member root credentials are present in {account['name']}.")


def inspect_root_management(config, api, account_ids, actions):
    service_access = {
        item["ServicePrincipal"]
        for item in api.collect(
            "organizations",
            "list_aws_service_access_for_organization",
            "EnabledServicePrincipals",
        )
    }
    if "iam.amazonaws.com" not in service_access:
        actions.append({"kind": "enable_iam_service_access"})
        enabled_features = set()
    else:
        enabled_features = set(api.client("iam").list_organizations_features()["EnabledFeatures"])
    for feature in config["root_access"]["features"]:
        if feature not in enabled_features:
            actions.append({"kind": "enable_root_feature", "feature": feature})

    delegated_name = config["root_access"]["delegated_administrator"]
    delegated = api.collect(
        "organizations",
        "list_delegated_administrators",
        "DelegatedAdministrators",
        ServicePrincipal="iam.amazonaws.com",
    )
    active_ids = {item["Id"] for item in delegated if item["Status"] == "ACTIVE"}
    desired_id = account_ids[delegated_name]
    if active_ids and active_ids != {desired_id}:
        fail("IAM root access is delegated to an unexpected account.")
    if not active_ids:
        actions.append(
            {
                "kind": "register_iam_delegated_administrator",
                "account_id": desired_id,
            }
        )


def inspect_service_control_policies(config, api, root, actions):
    organizations = api.client("organizations")
    policy_targets = {"$root": root["Id"]}
    policies = index_unique(
        api.collect(
            "organizations",
            "list_policies",
            "Policies",
            Filter="SERVICE_CONTROL_POLICY",
        ),
        "Name",
        "service control policy name",
    )
    for desired in config["service_control_policies"]:
        existing = policies.get(desired["name"])
        policy_id = existing.get("Id") if existing is not None else None
        if existing is not None and existing.get("AwsManaged"):
            fail(f"Desired SCP name is owned by AWS: {desired['name']}.")
        attached_targets = set()
        content_matches = False
        description_matches = False
        if existing is not None:
            details = organizations.describe_policy(PolicyId=policy_id)["Policy"]
            content_matches = json.loads(details["Content"]) == desired["document"]
            description_matches = (
                details["PolicySummary"].get("Description", "") == desired["description"]
            )
            attached_targets = {
                target["TargetId"]
                for target in api.collect(
                    "organizations",
                    "list_targets_for_policy",
                    "Targets",
                    PolicyId=policy_id,
                )
            }
        desired_targets = {policy_targets[target] for target in desired["targets"]}
        extra_targets = attached_targets - desired_targets
        if (
            existing is None
            or not content_matches
            or not description_matches
            or not desired_targets.issubset(attached_targets)
            or extra_targets
        ):
            actions.append(
                {
                    "kind": "ensure_service_control_policy",
                    "exists": existing is not None,
                    "policy_id": policy_id,
                    "name": desired["name"],
                    "description": desired["description"],
                    "document": desired["document"],
                    "needs_update": not content_matches or not description_matches,
                    "missing_targets": sorted(desired_targets - attached_targets),
                    "extra_targets": sorted(extra_targets),
                }
            )


def expected_landing_zone_manifest(config, account_ids):
    control_tower = config["control_tower"]
    retention = control_tower["log_retention_days"]
    bucket_configuration = {
        "loggingBucket": {"retentionDays": retention},
        "accessLoggingBucket": {"retentionDays": retention},
    }
    return {
        "accessManagement": {"enabled": control_tower["access_management_enabled"]},
        "securityRoles": {
            "accountId": account_ids[control_tower["security_account"]],
            "enabled": True,
        },
        "backup": {"enabled": control_tower["backup_enabled"]},
        "governedRegions": control_tower["governed_regions"],
        "config": {
            "accountId": account_ids[control_tower["config_account"]],
            "configurations": bucket_configuration,
            "enabled": True,
        },
        "centralizedLogging": {
            "accountId": account_ids[control_tower["log_archive_account"]],
            "configurations": bucket_configuration,
            "enabled": True,
        },
    }


def inspect_control_tower(config, api, ous, actions):
    control_tower = api.client("controltower")
    landing_zone = exactly_one(
        api.collect("controltower", "list_landing_zones", "landingZones"),
        "Control Tower landing zone",
    )
    landing_zone = control_tower.get_landing_zone(landingZoneIdentifier=landing_zone["arn"])[
        "landingZone"
    ]
    expected = config["control_tower"]
    if (
        landing_zone["status"] != "ACTIVE"
        or landing_zone["driftStatus"]["status"] != "IN_SYNC"
        or landing_zone["version"] != expected["version"]
        or landing_zone["latestAvailableVersion"] != expected["version"]
    ):
        fail("The Control Tower landing zone is not active, current, and in sync.")
    account_ids = {account["name"]: account["account_id"] for account in config["accounts"]}
    if landing_zone["manifest"] != expected_landing_zone_manifest(config, account_ids):
        fail("The Control Tower landing zone manifest differs from desired state.")

    baselines = index_unique(
        api.collect("controltower", "list_baselines", "baselines"),
        "name",
        "Control Tower baseline name",
    )
    enabled = api.collect(
        "controltower",
        "list_enabled_baselines",
        "enabledBaselines",
    )
    identity_arn = baselines["IdentityCenterBaseline"]["arn"]
    identity_enabled = exactly_one(
        [item for item in enabled if item["baselineIdentifier"] == identity_arn],
        "enabled Identity Center baseline",
    )
    control_tower_arn = baselines["AWSControlTowerBaseline"]["arn"]
    for ou_name, version in expected["enabled_baseline_ous"].items():
        target_arn = ous[ou_name]["Arn"]
        matches = [
            item
            for item in enabled
            if item["baselineIdentifier"] == control_tower_arn
            and item["targetIdentifier"] == target_arn
        ]
        if not matches:
            actions.append(
                {
                    "kind": "enable_control_tower_baseline",
                    "baseline_arn": control_tower_arn,
                    "baseline_version": version,
                    "identity_baseline_arn": identity_enabled["arn"],
                    "target_arn": target_arn,
                }
            )
            continue
        details = control_tower.get_enabled_baseline(
            enabledBaselineIdentifier=exactly_one(matches, f"enabled baseline for {ou_name}")["arn"]
        )["enabledBaselineDetails"]
        if details["baselineVersion"] != version:
            actions.append(
                {
                    "kind": "update_control_tower_baseline",
                    "baseline_version": version,
                    "enabled_baseline_arn": details["arn"],
                    "identity_baseline_arn": identity_enabled["arn"],
                }
            )


def inspect_foundation(config, api):
    expected_account_id = config["management_account_id"]
    if api.client("sts").get_caller_identity()["Account"] != expected_account_id:
        fail("Refusing organization operation outside the configured management account.")
    organization = api.client("organizations").describe_organization()["Organization"]
    management_id = organization.get("ManagementAccountId") or organization["MasterAccountId"]
    if management_id != expected_account_id:
        fail("The selected profile is not the organization management account.")
    if organization["FeatureSet"] != config["organization_feature_set"]:
        fail("The AWS organization feature set differs from desired state.")

    root = exactly_one(
        api.collect("organizations", "list_roots", "Roots"),
        "organization root",
    )
    ous, expected_accounts = inspect_accounts(
        config,
        api,
        api.client("organizations"),
        root,
    )
    inspect_root_credentials(config, api, expected_accounts)
    actions = []
    account_ids = {account["name"]: account["account_id"] for account in expected_accounts.values()}
    inspect_root_management(config, api, account_ids, actions)
    inspect_service_control_policies(config, api, root, actions)
    inspect_control_tower(config, api, ous, actions)
    return {
        "organization_id": organization["Id"],
        "management_account_id": expected_account_id,
        "actions": actions,
    }


def wait_for_baseline(control_tower, operation_id):
    for _ in range(180):
        operation = control_tower.get_baseline_operation(operationIdentifier=operation_id)[
            "baselineOperation"
        ]
        if operation["status"] == "SUCCEEDED":
            return
        if operation["status"] == "FAILED":
            fail(
                "Control Tower baseline operation failed: "
                f"{operation.get('statusMessage', 'no status message')}"
            )
        time.sleep(5)
    fail("Timed out waiting for the Control Tower baseline operation.")


def apply_actions(config, api, actions):
    organizations = api.client("organizations")
    iam = api.client("iam")
    control_tower = api.client("controltower")
    root_feature_operations = {
        "RootCredentialsManagement": iam.enable_organizations_root_credentials_management,
        "RootSessions": iam.enable_organizations_root_sessions,
    }
    for action in actions:
        kind = action["kind"]
        if kind == "enable_iam_service_access":
            organizations.enable_aws_service_access(ServicePrincipal="iam.amazonaws.com")
        elif kind == "enable_root_feature":
            root_feature_operations[action["feature"]]()
        elif kind == "register_iam_delegated_administrator":
            organizations.register_delegated_administrator(
                AccountId=action["account_id"],
                ServicePrincipal="iam.amazonaws.com",
            )
        elif kind == "ensure_service_control_policy":
            content = json.dumps(action["document"], separators=(",", ":"))
            if action["exists"] and action["needs_update"]:
                organizations.update_policy(
                    PolicyId=action["policy_id"],
                    Name=action["name"],
                    Description=action["description"],
                    Content=content,
                )
                policy_id = action["policy_id"]
            elif action["exists"]:
                policy_id = action["policy_id"]
            else:
                policy_id = organizations.create_policy(
                    Name=action["name"],
                    Description=action["description"],
                    Type="SERVICE_CONTROL_POLICY",
                    Content=content,
                )["Policy"]["PolicySummary"]["Id"]
            for target_id in action["missing_targets"]:
                organizations.attach_policy(PolicyId=policy_id, TargetId=target_id)
            for target_id in action["extra_targets"]:
                organizations.detach_policy(PolicyId=policy_id, TargetId=target_id)
        elif kind == "enable_control_tower_baseline":
            result = control_tower.enable_baseline(
                baselineIdentifier=action["baseline_arn"],
                baselineVersion=action["baseline_version"],
                targetIdentifier=action["target_arn"],
                parameters=[
                    {
                        "key": "IdentityCenterEnabledBaselineArn",
                        "value": action["identity_baseline_arn"],
                    }
                ],
            )
            wait_for_baseline(control_tower, result["operationIdentifier"])
        elif kind == "update_control_tower_baseline":
            result = control_tower.update_enabled_baseline(
                enabledBaselineIdentifier=action["enabled_baseline_arn"],
                baselineVersion=action["baseline_version"],
                parameters=[
                    {
                        "key": "IdentityCenterEnabledBaselineArn",
                        "value": action["identity_baseline_arn"],
                    }
                ],
            )
            wait_for_baseline(control_tower, result["operationIdentifier"])
        else:
            fail(f"Unsupported organization action: {kind}.")


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parents[2] / "organization" / "reaver-project.json",
    )
    return parser.parse_args(argv)


def main(argv=None, session_factory=None):
    arguments = parse_arguments(argv)
    config_path = arguments.config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    api = AwsApi(arguments.profile, config["home_region"], session_factory)
    state = inspect_foundation(config, api)
    current_plan = plan_contract.create(
        "aws-organization-foundation",
        config["management_account_id"],
        [config_path],
        state,
    )
    if arguments.mode == "plan":
        plan_contract.write(arguments.plan_file, current_plan)
        print(json.dumps(current_plan, indent=2, sort_keys=True))
        return
    reviewed_plan = plan_contract.read(arguments.plan_file)
    plan_contract.require_match(reviewed_plan, current_plan)
    print(json.dumps(reviewed_plan, indent=2, sort_keys=True))
    apply_actions(config, api, reviewed_plan["state"]["actions"])
