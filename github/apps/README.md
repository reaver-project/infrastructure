# GitHub Apps

The repository uses three private organization-owned GitHub Apps:

- the runner manager has only organization self-hosted-runner permission and
  is installed into the central AWS runner controller; and
- the infrastructure manager has the organization and repository permissions
  required to converge settings from this repository; and
- the maintenance manager has only the repository permissions required to
  publish maintenance branches and manage their pull requests.

The manifests are the reviewable source of truth for initial registration.
GitHub App creation itself is an interactive manifest handshake and cannot be
made fully convergent through the built-in workflow token. App installation,
permission changes, key rotation, and the resources that consume the keys are
managed after that bootstrap.

No webhook events are subscribed to. A direct manifest helper is sufficient;
introducing a Probot service would add a persistent web application without
providing a useful capability for this design.

Create a private temporary directory on a memory-backed filesystem, then write
only an encrypted credential bundle into it:

```console
credential_directory=$(mktemp -d /dev/shm/reaver-project-apps.XXXXXX)
github/apps/create github/apps/infrastructure/manifest.json \
    --output "${credential_directory}/infrastructure-app.json.gpg"
github/apps/create github/apps/maintenance/manifest.json \
    --output "${credential_directory}/maintenance-app.json.gpg"
github/apps/create github/apps/runner/manifest.json \
    --output "${credential_directory}/runner-app.json.gpg"
```

The helper binds only to loopback, validates the manifest `state`, exchanges
the one-hour GitHub code, and streams the response directly into symmetric
AES-256 encryption. It rejects output directories that are not `tmpfs` or
`ramfs`, rejects directories accessible by another user, creates the encrypted
output with mode `0600`, and refuses to overwrite it. The plaintext exists only
in the HTTPS, Python, GnuPG, pipe, and destination-process memory required for
the handoff. Installation into the organization is still an explicit owner
action in GitHub.

After installing the infrastructure App, use the owner's existing `gh`
credentials once to converge `github/repositories/infrastructure.json`, then
store the App credentials in the environment used by the manual configuration
workflow:

```console
github/configure-repository github/repositories/infrastructure.json
gpg --quiet --no-symkey-cache \
    --decrypt "${credential_directory}/infrastructure-app.json.gpg" \
    | github/apps/configure-infrastructure-app --credentials -
```

Subsequent organization and repository convergence uses the App, not the
owner's token. Creating the App and placing its first key remain bootstrap
operations because an App cannot create or initially credential itself.

Run the GitHub configuration workflow once to create the main-branch-only
`maintenance` environment in ReaverOS. Install the maintenance App into the
organization with access only to repositories that run trusted maintenance
workflows; initially this is `reaver-project/reaveros`. Accept the App's
`Workflows: write` permission so it can update pinned actions under
`.github/workflows`; the App remains outside every ruleset bypass list.

Place its narrow credential in ReaverOS's maintenance environment and in the
infrastructure repository's reviewed deployment environment. The former lets
ReaverOS publish its toolchain updates; the latter lets a completed AWS
deployment publish the corresponding contract update:

```console
gpg --quiet --no-symkey-cache \
    --decrypt "${credential_directory}/maintenance-app.json.gpg" \
    | github/apps/configure-maintenance-app \
        --credentials - \
        --repository reaver-project/reaveros
gpg --quiet --no-symkey-cache \
    --decrypt "${credential_directory}/maintenance-app.json.gpg" \
    | github/apps/configure-maintenance-app \
        --credentials - \
        --environment github-production \
        --repository reaver-project/infrastructure
```

Repository selection is installation policy rather than a separate App design.
Adding another maintained repository requires adding its protected environment,
granting it access in the existing App installation, and running the same
configurator with a different `--repository`; it does not require another App
or helper. Infrastructure needs only the credential, not an App installation on
its repository: deployment tokens are explicitly scoped to the consumer
repositories. The maintenance App is not a branch-protection bypass actor.

After installing the runner App, create or converge its organization runner
group and record the returned IDs:

```console
gpg --quiet --no-symkey-cache \
    --decrypt "${credential_directory}/runner-app.json.gpg" \
    | github/apps/configure-runner-group \
        --credentials - \
        --repository reaver-project/reaveros \
        --workflow reaver-project/reaveros/.github/workflows/aws-runner.yml@refs/heads/main
```

The group is visible only to its selected repositories and accepts runners
only for the selected workflow definitions. Its numeric ID is an input to the
ReaverOS AWS stack. Once the stack exists,
`projects/reaveros/aws/configure-runner-app` stores the App ID, installation
ID, and private key in Secrets Manager; pass its `--installation-id` from the
group configurator output, again decrypting only into a pipe:

```console
gpg --quiet --no-symkey-cache \
    --decrypt "${credential_directory}/runner-app.json.gpg" \
    | projects/reaveros/aws/configure-runner-app \
        --credentials - \
        --installation-id RUNNER_APP_INSTALLATION_ID
```

Application repository workflows never receive the Runner or Infrastructure
App credentials. Only a trusted default-branch maintenance job receives the
repository-scoped maintenance credential. After all destination stores have
been verified, remove the encrypted bundles and their temporary directory; no
App key was written to persistent storage:

```console
rm -- \
    "${credential_directory}/infrastructure-app.json.gpg" \
    "${credential_directory}/maintenance-app.json.gpg" \
    "${credential_directory}/runner-app.json.gpg"
rmdir -- "${credential_directory}"
```
