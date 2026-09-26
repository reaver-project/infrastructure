# ReaverOS infrastructure integration

This directory owns the cloud resources and shared integration contract used by
ReaverOS. ReaverOS itself continues to own build commands, CI task matrices,
spending authorization, build-environment cache keys, and ECR image promotion.
This repository owns the trusted GHCR publisher for successful ReaverOS main
builds. Its read-only ECR role is restricted to this repository's main-branch
publisher workflow; only that workflow receives a token able to publish the
two official GHCR build-environment packages.

An approved AWS deployment publishes eight non-secret repository variables to
`reaver-project/reaveros` through the infrastructure GitHub App:

- `AWS_REGION` identifies the region containing the runner stack;
- `AWS_INFRASTRUCTURE_CONTRACT_VERSION` records the version read back from the
  deployed stack;
- `AWS_INFRASTRUCTURE_REVISION` records the exact infrastructure commit used by
  the deployed change set;
- `AWS_RUNNER_STACK_NAME` identifies the stack whose outputs implement the
  contract;
- `AWS_RUNNER_ROLE_ARN` is the OIDC role used by runner and candidate-image
  workflows;
- `AWS_PRODUCTION_PROMOTION_ROLE_ARN` is the main-only OIDC role that writes
  production ECR images;
- `CI_GATE_APP_SLUG` identifies the App actor allowed to publish an approved
  pull request revision to a protected CI branch; and
- `MAINTENANCE_APP_SLUG` identifies the only App actor allowed to propose an
  automatic infrastructure update.

The publishing job runs only after the exact reviewed CloudFormation change
set succeeds. If publishing fails after AWS has deployed, use GitHub Actions'
"re-run failed jobs" operation; the successful deployment job does not need to
execute its one-use change set again. The local `configure-ci` helper provides
the same convergent operation for break-glass recovery.

`infrastructure-contract-version` is the source of truth for compatibility.
Compatible additions retain its value. A breaking change to stack outputs,
shared-action inputs or outputs, OIDC trust, or resource ownership increments
it and requires a coordinated consumer update pinned to an immutable commit of
this repository.

After the authoritative variables are published, a separately scoped
Maintenance App token runs `actions/update-infrastructure-consumer`. The action
updates every shared-action pin and contract input, publishes the ReaverOS pull
request, and enables auto-merge. Its consumer-side validation permits only
those semantic substitutions and compares them to the deployed variables;
repository rules remain the authority that decides whether the PR may merge.

ReaverOS-owned policy such as `AWS_CI_TRUSTED_USERS` is intentionally not set
here.

The runner role trusts `ci.yml` and its local AWS reusable workflows on `main`
and on `pull-request/*`. Repository rules reserve that latter namespace for the
CI Gate App, which creates a branch only after resolving an approved revision
to the pull request's current full head commit. This couples AWS authorization
to an immutable reviewed object without granting fork workflows cloud access.
The production promotion role trusts only the cache-promotion workflow called
from `main`; the PR-capable role cannot write production ECR images.

The deployment also publishes `AWS_GHCR_PUBLISHER_ROLE_ARN` as a variable on
`reaver-project/infrastructure`. The publisher reconciles GHCR from a
successful `ci.yml` run at ReaverOS's current signed main commit. It can be
triggered manually or by its hourly schedule. Package names are initially
reserved as private packages from this repository using its manual reservation
job; the organization package-creation policy is recorded in
`github/organizations/reaver-project.json`. GitHub does not expose that policy
through the supported API used by the configurator, so an owner must verify
its effective value in organization settings. Private package creation remains
enabled there, so this setting is not a deny-all control for new package names.
The official packages must grant Actions write access to infrastructure only,
with source-repository inheritance off. Package source attribution can point
to ReaverOS independently of that Actions access.
