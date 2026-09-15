#!/usr/bin/env python3

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaver_project_aws import cost_controls
from test_support import SessionFactory, client

ORGANIZATION_PATH = Path(__file__).with_name("reaver-project.json")
ORGANIZATION = json.loads(ORGANIZATION_PATH.read_text(encoding="utf-8"))
CONFIG_PATH = Path(__file__).with_name("cost-controls.json")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MANAGEMENT_ACCOUNT_ID = ORGANIZATION["management_account_id"]
ACCOUNT_IDS = {account["name"]: account["account_id"] for account in ORGANIZATION["accounts"]}


class CostState:
    def __init__(self, *, missing_budget=False, cost_tag_status="Active"):
        accounts = {"$management": MANAGEMENT_ACCOUNT_ID, **ACCOUNT_IDS}
        existing_budgets = []
        for budget_config in CONFIG["budgets"]:
            budget = cost_controls.desired_budget(budget_config, accounts)
            budget["BudgetLimit"]["Amount"] += ".0"
            existing_budgets.append(budget)
        if missing_budget:
            existing_budgets = [
                budget
                for budget in existing_budgets
                if budget["BudgetName"] != "reaver-project-ci-monthly"
            ]
        notifications = [
            notification["Notification"]
            for notification in cost_controls.desired_notifications(CONFIG, "billing@example.com")
        ]
        self.organizations = client(
            describe_organization={"Organization": {"MasterAccountId": MANAGEMENT_ACCOUNT_ID}},
            list_accounts={
                "Accounts": [
                    {"Name": name, "Id": account_id, "Status": "ACTIVE"}
                    for name, account_id in ACCOUNT_IDS.items()
                ]
            },
        )
        self.sts = client(get_caller_identity={"Account": MANAGEMENT_ACCOUNT_ID})
        self.budgets = client(
            describe_budgets={"Budgets": existing_budgets},
            describe_notifications_for_budget={"Notifications": notifications},
            describe_subscribers_for_notification={
                "Subscribers": [
                    {
                        "SubscriptionType": "EMAIL",
                        "Address": "billing@example.com",
                    }
                ]
            },
        )
        resource_tags = cost_controls.desired_resource_tags(CONFIG)
        self.cost_explorer = client(
            get_anomaly_monitors={
                "AnomalyMonitors": [
                    {
                        "MonitorName": "reaver-project-aws-services",
                        "MonitorType": "DIMENSIONAL",
                        "MonitorDimension": "SERVICE",
                        "MonitorArn": "arn:monitor",
                    }
                ]
            },
            get_anomaly_subscriptions={
                "AnomalySubscriptions": [
                    {
                        "SubscriptionName": "reaver-project-daily-cost-anomalies",
                        "SubscriptionArn": "arn:subscription",
                        "Frequency": "DAILY",
                        "Threshold": 5.0,
                        "MonitorArnList": ["arn:monitor"],
                        "Subscribers": [
                            {
                                "Address": "billing@example.com",
                                "Type": "EMAIL",
                                "Status": "CONFIRMED",
                            }
                        ],
                    }
                ]
            },
            list_tags_for_resource={"ResourceTags": resource_tags},
            list_cost_allocation_tags={
                "CostAllocationTags": (
                    []
                    if cost_tag_status is None
                    else [{"TagKey": "Project", "Status": cost_tag_status}]
                )
            },
            update_cost_allocation_tags_status={"Errors": []},
        )
        self.factory = SessionFactory(
            {
                "budgets": self.budgets,
                "ce": self.cost_explorer,
                "organizations": self.organizations,
                "sts": self.sts,
            }
        )


class CostControlTests(unittest.TestCase):
    def run_configure(self, mode, state):
        with tempfile.TemporaryDirectory() as directory:
            plan_file = Path(directory) / "plan.json"
            arguments = [
                "--profile",
                "management",
                "--plan-file",
                str(plan_file),
            ]
            with (
                patch(
                    "reaver_project_aws.plan_contract.repository_identity",
                    return_value="a" * 40,
                ),
                patch.dict(
                    os.environ,
                    {"AWS_BUDGET_NOTIFICATION_EMAIL": "billing@example.com"},
                ),
            ):
                plan_output = io.StringIO()
                with contextlib.redirect_stdout(plan_output):
                    cost_controls.main(["plan", *arguments], state.factory)
                if mode == "apply":
                    apply_output = io.StringIO()
                    with contextlib.redirect_stdout(apply_output):
                        cost_controls.main(["apply", *arguments], state.factory)
                    return json.loads(apply_output.getvalue())
                return json.loads(plan_output.getvalue())

    def test_current_cost_controls_are_converged(self):
        state = CostState()
        plan = self.run_configure("plan", state)
        self.assertEqual(plan["state"], {"actions": [], "pending": []})

    def test_missing_budget_is_planned(self):
        plan = self.run_configure("plan", CostState(missing_budget=True))
        action = plan["state"]["actions"][0]
        self.assertEqual(action["kind"], "ensure_budget")
        self.assertEqual(action["budget"]["BudgetName"], "reaver-project-ci-monthly")
        self.assertFalse(action["exists"])

    def test_unseen_cost_tag_is_pending(self):
        plan = self.run_configure("plan", CostState(cost_tag_status=None))
        self.assertEqual(plan["state"]["actions"], [])
        self.assertEqual(
            plan["state"]["pending"][0]["kind"],
            "await_cost_allocation_tag",
        )

    def test_inactive_cost_tag_is_activated_on_apply(self):
        state = CostState(cost_tag_status="Inactive")
        plan = self.run_configure("apply", state)
        self.assertEqual(
            plan["state"]["actions"][0]["kind"],
            "activate_cost_allocation_tag",
        )
        state.cost_explorer.update_cost_allocation_tags_status.assert_called_once_with(
            CostAllocationTagsStatus=[{"TagKey": "Project", "Status": "Active"}]
        )


if __name__ == "__main__":
    unittest.main()
