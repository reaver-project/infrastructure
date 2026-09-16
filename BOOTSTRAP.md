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
2. Create and install the Infrastructure and Runner Apps using
   `github/apps/README.md`. The Maintenance and CI Gate definitions remain in
   the repository, but ReaverOS repository installation is deferred.
3. Use the owner token once to run `github/configure-repository` with
   `github/repositories/infrastructure.json`. The configurator establishes the
   ruleset and protected environments before its final operation makes the
   repository public.
4. Install the Infrastructure App credential in the `github-production`
   environment with `github/apps/configure-infrastructure-app`.
5. Configure the Runner App group with `github/apps/configure-runner-group` and
   retain its numeric group and installation IDs.
6. In the log-archive account, run
   `aws/organization/log-archive/deploy plan` with the
   `reaver-project-log-archive-admin` profile. Account IDs come from the
   reviewed organization configuration. Inspect and apply that exact change
   set, then retain its `DeploymentPlanAuditLogBucketName` output.
7. Create and inspect the permanent CI control-plane change set:

   ```console
   aws/bootstrap/plan \
       --profile reaver-project-ci-admin \
       --deployment-plan-audit-bucket <central-audit-bucket>
   ```

8. Run `aws/bootstrap/apply` with the same profile and exact
   repository revision. It rejects a missing, altered, or stale change set and
   enables termination protection after the stack completes.
9. Run `aws/bootstrap/publish-github` with the same profile,
   together with the budget email and runner group ID. This is a separate
   GitHub mutation and does not modify AWS.
10. Merge the permanent control-plane workflows. Use the bootstrap entrypoints
    one final time to install their dedicated plan, deploy, and CloudFormation
    execution roles, then publish the new role outputs.
11. Make a reviewed control-plane change and let `Plan AWS control plane`
    create its private plan. Inspect it in AWS, manually dispatch
    `Deploy AWS control plane` with the opaque lookup key, and approve the
    environment. A successful deployment proves that the permanent stack owns
    and can update itself without the administrator profile.
12. Run the GitHub configuration workflow. It converges only organization and
    infrastructure-repository policy during bootstrap.
13. Let `Plan AWS infrastructure` create the runner-stack plan. Inspect and
    deploy it through `Deploy AWS infrastructure`; this workflow does not access
    or configure a ReaverOS repository.
14. Read the `CiGateWebhookUrl` stack output, create the CI Gate App, and install
    both CI Gate and Runner App credentials into the runner stack as documented
    in `github/apps/README.md`. The runner group remains locked and empty until
    a consumer repository is migrated.
15. Confirm the SNS subscription sent to the
   budget email address so controller and reaper alarms can notify you.
16. Rerun the organization cost-control plan. Once AWS Billing reports the
   `Project` tag as inactive rather than unseen, apply the plan to activate it
   for the tag-filtered ReaverOS budget.
17. Audit the handoff without consulting a ReaverOS repository:

   ```console
   aws/bootstrap/audit-completion \
       --profile reaver-project-ci-admin \
       --log-archive-profile reaver-project-log-archive-audit
   ```

   The audit requires permanent CloudFormation ownership, protected and current
   stacks, configured App secrets, matching GitHub variables and immutable OIDC
   claims, and successful permanent plan/deploy/configuration workflows at the
   deployed revision.
18. Remove temporary change sets, ephemeral SSO sessions, and encrypted App
   bundles from their memory-backed directory. Routine administrator-profile
   use ends here.

After this point bootstrap is complete. Creating or migrating
`reaver-project/reaveros`, granting App installations access to it, applying
`github/repositories/reaveros.json`, and publishing the infrastructure contract
are consumer-migration work rather than bootstrap work.

The permanent control-plane stack has termination protection. App keys must
never be written to persistent plaintext storage, and neither normal workflow
has access to an owner credential. Control Tower records organization-wide
management activity. The control plane's separate, single-Region trail records
only deployment-plan object access in an Object Lock-protected bucket owned by
the log-archive account, with one year of default retention and log-file
integrity validation.
