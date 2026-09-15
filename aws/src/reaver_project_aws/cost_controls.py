import argparse
import json
import os
import sys
from pathlib import Path

from . import plan_contract
from .api import AwsApi


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def require_management_account(api, expected_account_id):
    if api.client("sts").get_caller_identity()["Account"] != expected_account_id:
        fail("Refusing cost-control operation outside the configured management account.")
    organization = api.client("organizations").describe_organization()["Organization"]
    management_id = organization.get("ManagementAccountId") or organization["MasterAccountId"]
    if management_id != expected_account_id:
        fail("The selected profile is not the organization management account.")


def account_ids(api, organization_config):
    expected = {
        account["name"]: account["account_id"] for account in organization_config["accounts"]
    }
    result = {"$management": organization_config["management_account_id"]}
    for account in api.collect("organizations", "list_accounts", "Accounts"):
        if account["Status"] != "ACTIVE":
            continue
        if account["Name"] in result:
            fail(f"Duplicate active AWS account name: {account['Name']}.")
        if account["Name"] in expected and account["Id"] != expected[account["Name"]]:
            fail(f"AWS account ID differs from desired state: {account['Name']}.")
        result[account["Name"]] = account["Id"]
    return result


def desired_notifications(config, email):
    return [
        {
            "Notification": {
                "NotificationType": notification["type"],
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": notification["threshold"],
                "ThresholdType": "PERCENTAGE",
            },
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
        }
        for notification in config["notifications"]
    ]


def desired_budget(config, accounts):
    budget = {
        "BudgetName": config["name"],
        "BudgetLimit": {"Amount": str(config["amount"]), "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "BudgetType": "COST",
    }
    linked_account = config.get("linked_account")
    if linked_account is not None:
        if linked_account not in accounts:
            fail(f"Budget references unknown account: {linked_account}.")
        budget["FilterExpression"] = {
            "Dimensions": {
                "Key": "LINKED_ACCOUNT",
                "Values": [accounts[linked_account]],
                "MatchOptions": ["EQUALS"],
            }
        }
        budget["Metrics"] = ["UnblendedCost"]
    return budget


def budget_matches(actual, desired):
    return (
        actual["BudgetName"] == desired["BudgetName"]
        and float(actual["BudgetLimit"]["Amount"]) == float(desired["BudgetLimit"]["Amount"])
        and actual["BudgetLimit"]["Unit"] == "USD"
        and actual["TimeUnit"] == "MONTHLY"
        and actual["BudgetType"] == "COST"
        and actual.get("FilterExpression") == desired.get("FilterExpression")
        and actual.get("Metrics") == desired.get("Metrics")
    )


def notification_key(notification):
    return (
        notification["NotificationType"],
        notification["ComparisonOperator"],
        float(notification["Threshold"]),
    )


def notification_request(notification):
    return {
        "NotificationType": notification["NotificationType"],
        "ComparisonOperator": notification["ComparisonOperator"],
        "Threshold": notification["Threshold"],
        "ThresholdType": notification.get("ThresholdType", "PERCENTAGE"),
    }


def budget_notifications_match(
    budgets,
    management_account_id,
    budget_name,
    desired,
):
    actual = budgets.describe_notifications_for_budget(
        AccountId=management_account_id,
        BudgetName=budget_name,
    )["Notifications"]
    desired_by_key = {notification_key(item["Notification"]): item for item in desired}
    actual_by_key = {notification_key(item): item for item in actual}
    if set(actual_by_key) != set(desired_by_key):
        return False
    for key, notification in actual_by_key.items():
        subscribers = budgets.describe_subscribers_for_notification(
            AccountId=management_account_id,
            BudgetName=budget_name,
            Notification=notification_request(notification),
        )["Subscribers"]
        if subscribers != desired_by_key[key]["Subscribers"]:
            return False
    return True


def desired_resource_tags(config):
    return [
        {"Key": key, "Value": value}
        for key, value in sorted(config["anomaly_detection"]["resource_tags"].items())
    ]


def resource_tags_match(cost_explorer, resource_arn, desired):
    actual = cost_explorer.list_tags_for_resource(ResourceArn=resource_arn).get("ResourceTags", [])
    return all(tag in actual for tag in desired)


def inspect_budgets(config, api, management_account_id, email, actions):
    budgets = api.client("budgets")
    accounts = account_ids(api, config["organization"])
    existing = {
        budget["BudgetName"]: budget
        for budget in api.collect(
            "budgets",
            "describe_budgets",
            "Budgets",
            AccountId=management_account_id,
        )
    }
    notifications = desired_notifications(config["cost_controls"], email)
    for budget_config in config["cost_controls"]["budgets"]:
        budget = desired_budget(budget_config, accounts)
        current = existing.get(budget["BudgetName"])
        if (
            current is None
            or not budget_matches(current, budget)
            or not budget_notifications_match(
                budgets,
                management_account_id,
                budget["BudgetName"],
                notifications,
            )
        ):
            actions.append(
                {
                    "kind": "ensure_budget",
                    "exists": current is not None,
                    "budget": budget,
                    "notifications": notifications,
                }
            )


def inspect_anomaly_detection(config, api, email, actions, pending):
    cost_explorer = api.client("ce")
    anomaly = config["cost_controls"]["anomaly_detection"]
    resource_tags = desired_resource_tags(config["cost_controls"])
    monitors = [
        monitor
        for monitor in api.collect("ce", "get_anomaly_monitors", "AnomalyMonitors")
        if monitor["MonitorName"] == anomaly["monitor_name"]
    ]
    if len(monitors) > 1:
        fail(f"Duplicate cost anomaly monitor: {anomaly['monitor_name']}.")
    monitor = monitors[0] if monitors else None
    if monitor is not None and (
        monitor["MonitorType"] != "DIMENSIONAL" or monitor["MonitorDimension"] != "SERVICE"
    ):
        fail("The named cost anomaly monitor has an unexpected type.")
    if monitor is None:
        actions.append(
            {
                "kind": "create_anomaly_monitor",
                "name": anomaly["monitor_name"],
                "resource_tags": resource_tags,
            }
        )
    elif not resource_tags_match(cost_explorer, monitor["MonitorArn"], resource_tags):
        actions.append(
            {
                "kind": "ensure_resource_tags",
                "resource_arn": monitor["MonitorArn"],
                "resource_tags": resource_tags,
            }
        )

    subscriptions = [
        subscription
        for subscription in api.collect(
            "ce",
            "get_anomaly_subscriptions",
            "AnomalySubscriptions",
        )
        if subscription["SubscriptionName"] == anomaly["subscription_name"]
    ]
    if len(subscriptions) > 1:
        fail(f"Duplicate cost anomaly subscription: {anomaly['subscription_name']}.")
    subscription = subscriptions[0] if subscriptions else None
    monitor_arn = monitor.get("MonitorArn") if monitor is not None else None
    subscribers = [{"Address": email, "Type": "EMAIL"}]
    identities_match = (
        subscription is not None
        and [
            {"Address": item["Address"], "Type": item["Type"]}
            for item in subscription["Subscribers"]
        ]
        == subscribers
    )
    subscription_matches = identities_match and (
        subscription["Frequency"] == anomaly["frequency"]
        and float(subscription["Threshold"]) == float(anomaly["threshold"])
        and subscription["MonitorArnList"] == [monitor_arn]
    )
    if not subscription_matches:
        actions.append(
            {
                "kind": "ensure_anomaly_subscription",
                "exists": subscription is not None,
                "subscription_arn": (
                    subscription.get("SubscriptionArn") if subscription is not None else None
                ),
                "monitor_name": anomaly["monitor_name"],
                "monitor_arn": monitor_arn,
                "name": anomaly["subscription_name"],
                "frequency": anomaly["frequency"],
                "threshold": anomaly["threshold"],
                "subscribers": subscribers,
                "resource_tags": resource_tags,
            }
        )
        return
    if not all(
        subscriber.get("Status") == "CONFIRMED" for subscriber in subscription["Subscribers"]
    ):
        pending.append(
            {
                "kind": "await_anomaly_subscriber_confirmation",
                "subscription_name": anomaly["subscription_name"],
            }
        )
    if not resource_tags_match(
        cost_explorer,
        subscription["SubscriptionArn"],
        resource_tags,
    ):
        actions.append(
            {
                "kind": "ensure_resource_tags",
                "resource_arn": subscription["SubscriptionArn"],
                "resource_tags": resource_tags,
            }
        )


def inspect_cost_allocation_tags(config, api, actions, pending):
    cost_explorer = api.client("ce")
    for tag_key in config["cost_controls"]["cost_allocation_tags"]:
        tags = cost_explorer.list_cost_allocation_tags(TagKeys=[tag_key]).get(
            "CostAllocationTags", []
        )
        if not tags:
            pending.append(
                {
                    "kind": "await_cost_allocation_tag",
                    "tag_key": tag_key,
                    "reason": "AWS Billing has not observed this resource tag yet.",
                }
            )
        elif tags[0]["Status"] != "Active":
            actions.append({"kind": "activate_cost_allocation_tag", "tag_key": tag_key})


def inspect_cost_controls(cost_config, organization_config, api, email):
    management_account_id = organization_config["management_account_id"]
    combined = {
        "cost_controls": cost_config,
        "organization": organization_config,
    }
    actions = []
    pending = []
    inspect_budgets(combined, api, management_account_id, email, actions)
    inspect_anomaly_detection(combined, api, email, actions, pending)
    inspect_cost_allocation_tags(combined, api, actions, pending)
    return {"actions": actions, "pending": pending}


def reconcile_budget(budgets, management_account_id, action):
    name = action["budget"]["BudgetName"]
    if not action["exists"]:
        budgets.create_budget(
            AccountId=management_account_id,
            Budget=action["budget"],
            NotificationsWithSubscribers=action["notifications"],
        )
        return
    budgets.update_budget(
        AccountId=management_account_id,
        NewBudget=action["budget"],
    )
    for notification in budgets.describe_notifications_for_budget(
        AccountId=management_account_id,
        BudgetName=name,
    ).get("Notifications", []):
        budgets.delete_notification(
            AccountId=management_account_id,
            BudgetName=name,
            Notification=notification_request(notification),
        )
    for notification in action["notifications"]:
        budgets.create_notification(
            AccountId=management_account_id,
            BudgetName=name,
            Notification=notification["Notification"],
            Subscribers=notification["Subscribers"],
        )


def resolve_monitor_arn(api, monitor_name):
    matches = [
        monitor
        for monitor in api.collect("ce", "get_anomaly_monitors", "AnomalyMonitors")
        if monitor["MonitorName"] == monitor_name
    ]
    if len(matches) != 1:
        fail(f"Expected one cost anomaly monitor named {monitor_name}.")
    return matches[0]["MonitorArn"]


def apply_actions(api, management_account_id, actions):
    budgets = api.client("budgets")
    cost_explorer = api.client("ce")
    for action in actions:
        kind = action["kind"]
        if kind == "ensure_budget":
            reconcile_budget(budgets, management_account_id, action)
        elif kind == "create_anomaly_monitor":
            cost_explorer.create_anomaly_monitor(
                AnomalyMonitor={
                    "MonitorName": action["name"],
                    "MonitorType": "DIMENSIONAL",
                    "MonitorDimension": "SERVICE",
                },
                ResourceTags=action["resource_tags"],
            )
        elif kind == "ensure_anomaly_subscription":
            monitor_arn = action["monitor_arn"] or resolve_monitor_arn(api, action["monitor_name"])
            subscription = {
                "SubscriptionName": action["name"],
                "MonitorArnList": [monitor_arn],
                "Subscribers": action["subscribers"],
                "Threshold": action["threshold"],
                "Frequency": action["frequency"],
            }
            if action["exists"]:
                subscription["SubscriptionArn"] = action["subscription_arn"]
                cost_explorer.update_anomaly_subscription(AnomalySubscription=subscription)
                cost_explorer.tag_resource(
                    ResourceArn=action["subscription_arn"],
                    ResourceTags=action["resource_tags"],
                )
            else:
                cost_explorer.create_anomaly_subscription(
                    AnomalySubscription=subscription,
                    ResourceTags=action["resource_tags"],
                )
        elif kind == "ensure_resource_tags":
            cost_explorer.tag_resource(
                ResourceArn=action["resource_arn"],
                ResourceTags=action["resource_tags"],
            )
        elif kind == "activate_cost_allocation_tag":
            result = cost_explorer.update_cost_allocation_tags_status(
                CostAllocationTagsStatus=[{"TagKey": action["tag_key"], "Status": "Active"}]
            )
            if result.get("Errors"):
                fail(
                    "AWS did not activate the cost allocation tag: "
                    + json.dumps(result["Errors"], separators=(",", ":"))
                )
        else:
            fail(f"Unsupported cost-control action: {kind}.")


def parse_arguments(argv=None):
    base = Path(__file__).parents[2] / "organization"
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=base / "cost-controls.json",
    )
    parser.add_argument(
        "--organization-config",
        type=Path,
        default=base / "reaver-project.json",
    )
    return parser.parse_args(argv)


def main(argv=None, session_factory=None):
    arguments = parse_arguments(argv)
    config_path = arguments.config.resolve()
    organization_path = arguments.organization_config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    organization = json.loads(organization_path.read_text(encoding="utf-8"))
    management_id = organization["management_account_id"]
    email_environment = config["notification_email_environment"]
    email = os.environ.get(email_environment)
    if not email:
        fail(f"Required environment variable is empty: {email_environment}.")
    api = AwsApi(arguments.profile, organization["home_region"], session_factory)
    require_management_account(api, management_id)
    state = inspect_cost_controls(config, organization, api, email)
    current_plan = plan_contract.create(
        "aws-organization-cost-controls",
        management_id,
        [config_path, organization_path],
        state,
    )
    if arguments.mode == "plan":
        plan_contract.write(arguments.plan_file, current_plan)
        print(json.dumps(current_plan, indent=2, sort_keys=True))
        return
    reviewed_plan = plan_contract.read(arguments.plan_file)
    plan_contract.require_match(reviewed_plan, current_plan)
    print(json.dumps(reviewed_plan, indent=2, sort_keys=True))
    apply_actions(api, management_id, reviewed_plan["state"]["actions"])
