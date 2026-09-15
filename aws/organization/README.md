# AWS organization desired state

The AWS management account, organization, member accounts, IAM Identity Center
directory, and initial Control Tower landing zone are external prerequisites.
This directory does not attempt to bootstrap or delete them.

`reaver-project.json` describes their expected topology and the organization
features managed after bootstrap. `configure.py plan` audits that topology and
prints the exact convergent operations still required. `configure.py apply`
performs only those operations. Both commands require an explicit management
account profile and expected account ID.

The initial desired state enables centralized member root management, delegates
it to the security account, verifies the Control Tower 4.0 manifest, and enrolls
the Deployments OU with `AWSControlTowerBaseline` version 5.0. Missing accounts,
missing OUs, unexpected delegation, landing-zone drift, and account placement
differences are errors rather than implicitly destructive repairs.
