# ReaverOS infrastructure integration

This directory owns the cloud resources and shared integration contract used by
ReaverOS. ReaverOS itself continues to own build commands, CI task matrices,
spending authorization, build-environment cache keys, and image promotion.

An approved AWS deployment publishes seven non-secret repository variables to
`reaver-project/reaveros` through the infrastructure GitHub App:

- `AWS_REGION` identifies the region containing the runner stack;
- `AWS_INFRASTRUCTURE_CONTRACT_VERSION` records the version read back from the
  deployed stack;
- `AWS_INFRASTRUCTURE_REVISION` records the exact infrastructure commit used by
  the deployed change set;
- `AWS_RUNNER_STACK_NAME` identifies the stack whose outputs implement the
  contract;
- `AWS_RUNNER_ROLE_ARN` is the narrowly trusted OIDC role used by ReaverOS
  workflows; and
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
