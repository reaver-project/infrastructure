#!/usr/bin/env python3

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaver_project_aws import organization
from test_support import SessionFactory, client

CONFIG_PATH = Path(__file__).with_name("reaver-project.json")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MANAGEMENT_ACCOUNT_ID = CONFIG["management_account_id"]
ACCOUNTS = {item["name"]: item["account_id"] for item in CONFIG["accounts"]}
ORGANIZATION_ID = "o-example"
ROOT_ID = "r-root"


class Foundation:
    def __init__(self, *, caller_account_id=MANAGEMENT_ACCOUNT_ID, extra_policy_target=None):
        self.ou_ids = {
            name: f"ou-{index}" for index, name in enumerate(CONFIG["organizational_units"])
        }
        self.organizations = client(
            describe_organization={
                "Organization": {
                    "Id": ORGANIZATION_ID,
                    "MasterAccountId": MANAGEMENT_ACCOUNT_ID,
                    "FeatureSet": "ALL",
                }
            },
            list_roots={"Roots": [{"Id": ROOT_ID}]},
            list_organizational_units_for_parent={
                "OrganizationalUnits": [
                    {
                        "Name": name,
                        "Id": identifier,
                        "Arn": (
                            "arn:aws:organizations::"
                            f"{MANAGEMENT_ACCOUNT_ID}:ou/{ORGANIZATION_ID}/{identifier}"
                        ),
                    }
                    for name, identifier in self.ou_ids.items()
                ]
            },
            list_accounts={
                "Accounts": [
                    {"Name": name, "Id": account_id, "Status": "ACTIVE"}
                    for name, account_id in ACCOUNTS.items()
                ]
            },
            list_aws_service_access_for_organization={
                "EnabledServicePrincipals": [{"ServicePrincipal": "iam.amazonaws.com"}]
            },
            list_delegated_administrators={
                "DelegatedAdministrators": [
                    {"Id": ACCOUNTS["reaver-project-security"], "Status": "ACTIVE"}
                ]
            },
            list_policies={
                "Policies": [
                    {
                        "Name": "DenyLeaveAndCloseAccount",
                        "Id": "p-deny-leave",
                        "AwsManaged": False,
                    }
                ]
            },
            describe_policy={
                "Policy": {
                    "PolicySummary": {
                        "Description": CONFIG["service_control_policies"][0]["description"]
                    },
                    "Content": json.dumps(CONFIG["service_control_policies"][0]["document"]),
                }
            },
            list_targets_for_policy={
                "Targets": [
                    {"TargetId": target}
                    for target in (ROOT_ID, extra_policy_target)
                    if target is not None
                ]
            },
        )
        parent_by_account = {
            account["account_id"]: self.ou_ids[account["organizational_unit"]]
            for account in CONFIG["accounts"]
        }
        self.organizations.list_parents.side_effect = lambda ChildId: {
            "Parents": [{"Id": parent_by_account[ChildId]}]
        }
        self.iam = client(
            get_account_summary={
                "SummaryMap": {
                    "AccountPasswordPresent": 1,
                    "AccountAccessKeysPresent": 0,
                    "AccountSigningCertificatesPresent": 0,
                    "AccountMFAEnabled": 1,
                }
            },
            list_organizations_features={
                "EnabledFeatures": ["RootCredentialsManagement", "RootSessions"]
            },
        )
        self.root_iam = client(
            get_account_summary={
                "SummaryMap": {
                    "AccountPasswordPresent": 0,
                    "AccountAccessKeysPresent": 0,
                    "AccountSigningCertificatesPresent": 0,
                    "AccountMFAEnabled": 0,
                }
            }
        )
        self.sts = client(
            get_caller_identity={"Account": caller_account_id},
            assume_root={
                "Credentials": {
                    "AccessKeyId": "temporary-access-key",
                    "SecretAccessKey": "temporary-secret-key",
                    "SessionToken": "temporary-session-token",
                }
            },
        )
        manifest = {
            "accessManagement": {"enabled": True},
            "securityRoles": {
                "accountId": ACCOUNTS["reaver-project-security"],
                "enabled": True,
            },
            "backup": {"enabled": False},
            "governedRegions": ["us-west-2"],
            "config": {
                "accountId": ACCOUNTS["reaver-project-security"],
                "configurations": {
                    "loggingBucket": {"retentionDays": 365},
                    "accessLoggingBucket": {"retentionDays": 365},
                },
                "enabled": True,
            },
            "centralizedLogging": {
                "accountId": ACCOUNTS["reaver-project-log-archive"],
                "configurations": {
                    "loggingBucket": {"retentionDays": 365},
                    "accessLoggingBucket": {"retentionDays": 365},
                },
                "enabled": True,
            },
        }
        self.control_tower = client(
            list_landing_zones={"landingZones": [{"arn": "arn:landing-zone"}]},
            get_landing_zone={
                "landingZone": {
                    "version": "4.0",
                    "latestAvailableVersion": "4.0",
                    "status": "ACTIVE",
                    "driftStatus": {"status": "IN_SYNC"},
                    "manifest": manifest,
                }
            },
            list_baselines={
                "baselines": [
                    {"name": "IdentityCenterBaseline", "arn": "arn:identity-baseline"},
                    {"name": "AWSControlTowerBaseline", "arn": "arn:control-tower-baseline"},
                ]
            },
            list_enabled_baselines={
                "enabledBaselines": [
                    {
                        "arn": "arn:enabled-identity-baseline",
                        "baselineIdentifier": "arn:identity-baseline",
                        "targetIdentifier": "arn:management-account",
                    }
                ]
            },
            enable_baseline={"operationIdentifier": "operation-1"},
            get_baseline_operation={"baselineOperation": {"status": "SUCCEEDED"}},
        )
        self.factory = SessionFactory(
            {
                "controltower": self.control_tower,
                "iam": self.iam,
                "organizations": self.organizations,
                "sts": self.sts,
            },
            self.root_iam,
        )


class ConfigureTests(unittest.TestCase):
    def run_configure(self, mode, foundation):
        with tempfile.TemporaryDirectory() as directory:
            plan_file = Path(directory) / "plan.json"
            arguments = [
                "--profile",
                "management",
                "--plan-file",
                str(plan_file),
            ]
            with patch(
                "reaver_project_aws.plan_contract.repository_identity",
                return_value="a" * 40,
            ):
                plan_output = io.StringIO()
                with contextlib.redirect_stdout(plan_output):
                    organization.main(["plan", *arguments], foundation.factory)
                self.assertEqual(plan_file.stat().st_mode & 0o777, 0o600)
                if mode == "apply":
                    apply_output = io.StringIO()
                    with contextlib.redirect_stdout(apply_output):
                        organization.main(["apply", *arguments], foundation.factory)
                    return json.loads(apply_output.getvalue())
                return json.loads(plan_output.getvalue())

    def test_plan_enrolls_only_the_deployments_ou(self):
        foundation = Foundation()
        plan = self.run_configure("plan", foundation)
        actions = plan["state"]["actions"]
        self.assertEqual([action["kind"] for action in actions], ["enable_control_tower_baseline"])
        self.assertEqual(actions[0]["baseline_version"], "5.0")
        self.assertEqual(foundation.sts.assume_root.call_count, 3)
        foundation.sts.assume_root.assert_any_call(
            DurationSeconds=300,
            TargetPrincipal=ACCOUNTS["reaver-project-ci"],
            TaskPolicyArn={"arn": organization.ROOT_AUDIT_POLICY_ARN},
        )
        foundation.control_tower.enable_baseline.assert_not_called()

    def test_apply_waits_for_control_tower(self):
        foundation = Foundation()
        self.run_configure("apply", foundation)
        foundation.control_tower.enable_baseline.assert_called_once()
        foundation.control_tower.get_baseline_operation.assert_called_once_with(
            operationIdentifier="operation-1"
        )

    def test_apply_removes_an_unmanaged_policy_attachment(self):
        foundation = Foundation(extra_policy_target="ou-unexpected")
        plan = self.run_configure("apply", foundation)
        policy_action = plan["state"]["actions"][0]
        self.assertEqual(policy_action["kind"], "ensure_service_control_policy")
        self.assertEqual(policy_action["extra_targets"], ["ou-unexpected"])
        foundation.organizations.update_policy.assert_not_called()
        foundation.organizations.detach_policy.assert_called_once_with(
            PolicyId="p-deny-leave",
            TargetId="ou-unexpected",
        )

    def test_rejects_the_wrong_management_account(self):
        foundation = Foundation(caller_account_id="999999999999")
        with self.assertRaises(SystemExit):
            self.run_configure("plan", foundation)
        foundation.control_tower.enable_baseline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
