# CI infrastructure bootstrap

This runbook establishes the external GitHub identity and creates the permanent
CI-account control plane consumed by the repository. The bootstrap entrypoints
delegate to the same control-plane implementation used after handoff; they do
not create a separate bootstrap stack or any bootstrap-only AWS resources.

## Prerequisites

- Create `reaver-project/infrastructure` as a private repository and seed its
  `main` branch with the reviewed infrastructure history.
- Establish the AWS organization and landing zone outside this bootstrap, and
  enroll the dedicated CI account before creating its deployment trust.
- Create the `infrastructure-maintainers` organization team outside this
  repository, grant it read access to both managed repositories, and maintain
  its membership separately.
- Keep owner-level GitHub and AWS credentials outside GitHub Actions.

## Procedure

1. Audit the existing organization and inspect its proposed convergent actions:

   ```console
   uv run --project aws --frozen reaver-project-aws-organization plan \
       --profile reaver-project-management-admin \
       --plan-file /dev/shm/reaver-project-organization.plan.json
   ```

   Apply that exact plan file from the same revision, replacing `plan` with
   `apply`. The command rejects changed live state, desired state, and source
   revisions. This enrolls the Deployments OU before any CI trust exists. Then
   run the cost-control plan with the same profile:

   ```console
   AWS_BUDGET_NOTIFICATION_EMAIL=<email> \
       uv run --project aws --frozen reaver-project-aws-cost-controls plan \
       --profile reaver-project-management-admin \
       --plan-file /dev/shm/reaver-project-cost-controls.plan.json
   ```

   Apply any actions after review. The `Project` allocation tag is expected to
   remain pending until AWS Billing observes the first tagged project resource.
2. Create and install the Infrastructure, Maintenance, and Runner Apps using
   `github/apps/README.md`.
3. Use the owner token once to run `github/configure-repository` with
   `github/repositories/infrastructure.json`. The configurator establishes the
   ruleset and protected environments before its final operation makes the
   repository public.
4. Install the Infrastructure App credential in the `github-production`
   environment with `github/apps/configure-infrastructure-app`.
5. Run the GitHub configuration workflow once. This converges ReaverOS policy
   and creates its main-branch-only `maintenance` environment.
6. Install the Maintenance App credential in that environment with
   `github/apps/configure-maintenance-app`.
7. Configure the Runner App group with `github/apps/configure-runner-group` and
   retain its numeric group and installation IDs.
8. In the log-archive account, run
   `aws/organization/log-archive/deploy plan` with the
   `reaver-project-log-archive-admin` profile. Account IDs come from the
   reviewed organization configuration. Inspect and apply that exact change
   set, then retain its `DeploymentPlanAuditLogBucketName` output.
9. Create and inspect the permanent CI control-plane change set:

   ```console
   aws/bootstrap/plan \
       --profile reaver-project-ci-admin \
       --deployment-plan-audit-bucket <central-audit-bucket>
   ```

10. Run `aws/bootstrap/apply` with the same profile and exact
   repository revision. It rejects a missing, altered, or stale change set and
   enables termination protection after the stack completes.
11. Run `aws/bootstrap/publish-github` with the same profile,
   together with the budget email and runner group ID. This is a separate
   GitHub mutation and does not modify AWS.
12. Let the main-branch AWS planning workflow create a private change set and
   metadata record in AWS. Inspect it in AWS, then manually dispatch the
   deployment workflow with the opaque lookup key and approve the environment.
   The first deployment can finish AWS successfully and then stop when it tries
   to publish the not-yet-created CI Gate App identity. Leave the successful
   deployment job intact.
13. Read the `CiGateWebhookUrl` stack output, create and install the CI Gate App,
   and run `github/apps/configure-ci-gate-app` as documented in
   `github/apps/README.md`. Re-run only the failed deployment-workflow jobs to
   publish the complete ReaverOS contract.
14. After the runner stack is deployed, confirm the SNS subscription sent to the
   budget email address so controller and reaper alarms can notify you.
15. Rerun the organization cost-control plan. Once AWS Billing reports the
   `Project` tag as inactive rather than unseen, apply the plan to activate it
   for the tag-filtered ReaverOS budget.
16. Stream the Runner App credential bundle into
   `projects/reaveros/aws/configure-runner-app` after the runner stack exists.
17. Run the GitHub configuration workflow to converge organization and
   repository policy through the Infrastructure App.

The permanent control-plane stack has termination protection. App keys must
never be written to persistent plaintext storage, and neither normal workflow
has access to an owner credential. Control Tower records organization-wide
management activity. The control plane's separate, single-Region trail records
only deployment-plan object access in an Object Lock-protected bucket owned by
the log-archive account, with one year of default retention and log-file
integrity validation.
