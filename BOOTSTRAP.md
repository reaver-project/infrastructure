# CI infrastructure bootstrap

This runbook establishes the external GitHub and CI-account deployment roots of
trust consumed by the repository. It is deliberately not automated by the
workflows whose credentials it creates.

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
   python3 aws/organization/configure.py plan \
       --profile reaver-management-admin \
       --expected-account-id <management-account-id>
   ```

   Apply the same revision only after reviewing that plan, replacing `plan`
   with `apply`. This enrolls the Deployments OU before any CI trust exists.
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
8. Create and inspect the CI deployment-trust change set:

   ```console
   aws/bootstrap/plan \
       --profile reaver-ci-admin \
       --expected-account-id <ci-account-id>
   ```

9. Run `aws/bootstrap/apply` with the same profile, account ID, and exact
   repository revision. It rejects a missing, altered, or stale change set and
   enables termination protection after the stack completes.
10. Run `aws/bootstrap/publish-github` with the same profile and account ID,
   together with the budget email and runner group ID. This is a separate
   GitHub mutation and does not modify AWS.
11. Let the main-branch AWS planning workflow create a private change set and
   metadata record in AWS. Inspect it in AWS, then manually dispatch the
   deployment workflow with the opaque lookup key and approve the environment.
12. Stream the Runner App credential bundle into
   `projects/reaveros/aws/configure-runner-app` after the runner stack exists.
13. Run the GitHub configuration workflow to converge organization and
   repository policy through the Infrastructure App.

The bootstrap stack has termination protection. App keys must never be written
to persistent plaintext storage, and neither normal workflow has access to an
owner credential.
