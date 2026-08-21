# ReaverOS infrastructure integration

This directory owns the cloud resources and shared integration contract used by
ReaverOS. ReaverOS itself continues to own build commands, CI task matrices,
spending authorization, build-environment cache keys, and image promotion.

An approved AWS deployment publishes three non-secret repository variables to
`reaver-project/reaveros` through the infrastructure GitHub App:

- `AWS_REGION` identifies the region containing the runner stack;
- `AWS_RUNNER_STACK_NAME` identifies the stack whose outputs implement the
  contract; and
- `AWS_RUNNER_ROLE_ARN` is the narrowly trusted OIDC role used by ReaverOS
  workflows.

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

ReaverOS-owned policy such as `AWS_CI_TRUSTED_USERS` is intentionally not set
here.
