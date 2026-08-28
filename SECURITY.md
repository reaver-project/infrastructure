# Security policy

## Supported versions

Only the current `main` branch is supported. This repository describes a live
control plane, so fixes are made against current desired state rather than
backported to older revisions.

## Reporting a vulnerability

Report vulnerabilities through [GitHub private vulnerability reporting][report]
instead of opening a public issue. Include the affected file or component, the
security boundary that can be crossed, reproduction details, and any known
mitigation. Do not include active credentials or unrelated personal data.

This project is maintained by one person and cannot promise a fixed response
time. Reports will be acknowledged and triaged as promptly as practical. If a
report identifies exposed credentials, assume they must be revoked and rotated;
do not continue using them to demonstrate impact.

Operational outages, ordinary defects, and hardening suggestions without a
confidentiality requirement belong in the public issue tracker.

[report]: https://github.com/reaver-project/infrastructure/security/advisories/new
