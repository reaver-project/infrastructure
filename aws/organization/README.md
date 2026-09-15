# AWS organization desired state

The AWS management account, organization, member accounts, IAM Identity Center
directory, and initial Control Tower landing zone are external prerequisites.
This directory does not attempt to bootstrap or delete them.

`reaver-project.json` describes their expected topology and the organization
features managed after bootstrap. Run `reaver-project-aws-organization` through
the locked `aws` uv project. Its `plan` mode audits that topology and writes a
private, revision-bound plan; its `apply` mode accepts only that exact plan and
performs its recorded operations. The configured management account ID is
checked through the AWS API rather than accepted as a command-line assertion.

The initial desired state enables centralized member root management, delegates
it to the security account, verifies the Control Tower 4.0 manifest, and enrolls
the Deployments OU with `AWSControlTowerBaseline` version 5.0. Missing accounts,
missing OUs, unexpected delegation, landing-zone drift, and account placement
differences are errors rather than implicitly destructive repairs.

`cost-controls.json` and `reaver-project-aws-cost-controls` manage consolidated
organization and linked-account budgets, their notification subscribers,
organization-wide cost anomaly detection, and cost allocation tag activation.
A tag that AWS Billing has not observed yet is reported as pending rather than
treated as a failure. Rerun the cost-control plan after the first tagged project
resources are created, then apply it to activate the tag.
