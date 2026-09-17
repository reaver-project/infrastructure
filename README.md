# Reaver Project infrastructure

This repository contains the shared infrastructure control plane for the
Reaver Project. It defines AWS and GitHub desired state, deployment workflows,
reusable integration actions, and project-specific infrastructure that does not
belong in an application repository.

Application repositories retain their own build and test semantics. For
example, ReaverOS owns toolchain and image construction, cache keys, test
matrices, and image promotion while consuming the runner platform and versioned
infrastructure contract defined here.

## Components

The AWS configuration covers:

- organization topology, centralized root access, service control policies,
  Control Tower enrollment, budgets, and cost monitoring;
- central audit storage for infrastructure deployment plans;
- GitHub OIDC trust, deployment roles, permissions boundaries, and artifact
  storage; and
- the ReaverOS ephemeral-runner platform, including its network, controllers,
  build-environment registries, and monitoring resources.

The GitHub configuration covers:

- organization and repository settings, rulesets, environments, Actions
  permissions, and OIDC subject claims;
- Infrastructure, Maintenance, Runner, and CI Gate App definitions; and
- reusable actions for stack contracts, ephemeral runners, and infrastructure
  consumer updates.

## Repository layout

- `aws/control-plane/`: permanent CI-account deployment trust and its management tools;
- `aws/bootstrap/`: thin first-install and recovery entrypoints for the control plane;
- `aws/organization/`: AWS organization and cost-control desired state;
- `actions/`: reusable GitHub Actions integrations;
- `github/apps/`: GitHub App manifests and credential bootstrap tools;
- `github/organizations/`: organization-wide GitHub desired state;
- `github/repositories/`: repository-specific GitHub desired state;
- `projects/reaveros/aws/`: the ReaverOS runner platform and deployment tools;
- `projects/reaveros/`: the ReaverOS infrastructure contract and integration;
  and
- `schemas/`: JSON schemas for repository-owned configuration formats.

## Infrastructure contracts

Project integrations declare an integer infrastructure contract version and an
immutable infrastructure source revision. Consumers validate both values before
using shared actions or cloud resources. The ReaverOS contract version is stored
in `projects/reaveros/infrastructure-contract-version` and is also published by
its CloudFormation stack.

## Development

Install the repository validation hook and its isolated tool environments with:

```console
pre-commit install --install-hooks
```

Run the complete local validation suite with:

```console
pre-commit run --all-files
pre-commit run gitleaks-history --hook-stage manual --all-files
```

`./test` runs the repository's unit and integration tests without invoking the
complete static-analysis toolchain.

## Documentation

- `BOOTSTRAP.md` describes initial AWS, GitHub, and GitHub App setup.
- `aws/organization/README.md` describes the AWS organization desired state.
- `github/apps/README.md` describes GitHub App creation and credential handling.
- `projects/reaveros/README.md` describes the ReaverOS integration.
- `SECURITY.md` describes vulnerability reporting.
