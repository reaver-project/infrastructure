import base64
import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

old_revision = "1" * 40

module_path = Path(__file__).with_name("lib") / "infrastructure_consumer.py"
specification = importlib.util.spec_from_file_location("infrastructure_consumer", module_path)
infrastructure_consumer = importlib.util.module_from_spec(specification)
specification.loader.exec_module(infrastructure_consumer)


def git(root, *arguments, capture_output=False):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=root,
        check=True,
        capture_output=capture_output,
        env=infrastructure_consumer.isolated_git_environment(),
        text=capture_output,
    )


class UpdateInfrastructureConsumerTests(unittest.TestCase):
    def test_publishes_and_enables_auto_merge_for_a_contract_update(self):
        repository_root = Path(__file__).resolve().parents[1]
        source_revision = git(
            repository_root,
            "rev-parse",
            "HEAD",
            capture_output=True,
        ).stdout.strip()
        contract_version = (
            (repository_root / "projects/reaveros/infrastructure-contract-version")
            .read_text(encoding="utf-8")
            .strip()
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source = temporary_path / "consumer-source"
            remote = temporary_path / "consumer.git"
            source.mkdir()
            (source / ".github" / "workflows").mkdir(parents=True)
            (source / "ci" / "aws").mkdir(parents=True)
            (source / "ci" / "aws" / "infrastructure-revision").write_text(
                f"{old_revision}\n", encoding="utf-8"
            )
            (source / "ci" / "aws" / "infrastructure-contract-version").write_text(
                "1\n", encoding="utf-8"
            )
            (source / ".github" / "workflows" / "ci.yml").write_text(
                f"""name: CI
jobs:
    contract:
        uses: reaver-project/infrastructure/actions/aws-stack-contract@{old_revision}
        with:
            expected-contract-version: '1'
""",
                encoding="utf-8",
            )
            git(source, "init", "--quiet", "-b", "main")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.com")
            git(source, "add", "--all")
            git(source, "commit", "--quiet", "--no-gpg-sign", "-m", "Base")
            git(temporary_path, "clone", "--quiet", "--bare", str(source), str(remote))

            mock_directory = temporary_path / "bin"
            mock_directory.mkdir()
            mock_gh = mock_directory / "gh"
            mock_gh.write_text(
                """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys

arguments = sys.argv[1:]
log = pathlib.Path(os.environ["TEST_GH_LOG"])
with log.open("a", encoding="utf-8") as output:
    print("\\t".join(arguments), file=output)

if arguments[:2] == ["auth", "setup-git"]:
    sys.exit()
if arguments[:2] == ["repo", "view"]:
    print("main")
    sys.exit()
if arguments[:2] == ["repo", "clone"]:
    subprocess.run(
        ["git", "clone", os.environ["TEST_CONSUMER_REMOTE"], arguments[3], *arguments[5:]],
        check=True,
    )
    sys.exit()
if arguments[0] == "api":
    path = arguments[1]
    if path.startswith("repos/reaver-project/reaveros/git/ref/heads/"):
        if not pathlib.Path(os.environ["TEST_BRANCH_CREATED"]).exists():
            sys.exit(1)
        print(os.environ["TEST_BASE_REVISION"])
        sys.exit()
    if path == "repos/reaver-project/reaveros/git/refs":
        pathlib.Path(os.environ["TEST_BRANCH_CREATED"]).touch()
        sys.exit()
    if path == "graphql":
        request_file = arguments[arguments.index("--input") + 1]
        pathlib.Path(os.environ["TEST_GRAPHQL_PAYLOAD"]).write_text(
            pathlib.Path(request_file).read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(json.dumps({"data": {"createCommitOnBranch": {"commit": {
            "oid": "a" * 40,
            "signature": {"isValid": True, "wasSignedByGitHub": True,
                "signer": {"login": "web-flow"}},
            "author": {"user": {"login": "reaver-project-maintenance[bot]"}}
        }}}}))
        sys.exit()
if arguments[:2] == ["pr", "create"]:
    pathlib.Path(os.environ["TEST_PR_CREATED"]).touch()
    sys.exit()
if arguments[:2] == ["pr", "list"]:
    if "--head" in arguments:
        if pathlib.Path(os.environ["TEST_PR_CREATED"]).exists():
            print("17")
    else:
        print(f"17\\tmaintenance/infrastructure/{os.environ['GITHUB_SHA']}")
    sys.exit()
if arguments[:2] in (["pr", "merge"], ["pr", "edit"], ["pr", "close"]):
    sys.exit()
raise SystemExit(f"unexpected gh invocation: {arguments}")
""",
                encoding="utf-8",
            )
            mock_gh.chmod(mock_gh.stat().st_mode | stat.S_IXUSR)

            output = temporary_path / "github-output"
            summary = temporary_path / "github-summary"
            gh_log = temporary_path / "gh-log"
            output.touch()
            summary.touch()
            gh_log.touch()
            environment = infrastructure_consumer.isolated_git_environment()
            base_revision = git(source, "rev-parse", "HEAD", capture_output=True).stdout.strip()
            graphql_payload = temporary_path / "graphql-payload.json"
            environment.update(
                {
                    "APP_SLUG": "reaver-project-maintenance",
                    "CONSUMER_REPOSITORY": "reaver-project/reaveros",
                    "CONTRACT_VERSION": contract_version,
                    "CONTRACT_VERSION_FILE": "ci/aws/infrastructure-contract-version",
                    "GH_TOKEN": "test-token",
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_SHA": source_revision,
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "INFRASTRUCTURE_REVISION": source_revision,
                    "PATH": f"{mock_directory}:{environment['PATH']}",
                    "REVISION_FILE": "ci/aws/infrastructure-revision",
                    "SOURCE_CONTRACT_VERSION_FILE": (
                        "projects/reaveros/infrastructure-contract-version"
                    ),
                    "TEST_CONSUMER_REMOTE": str(remote),
                    "TEST_BASE_REVISION": base_revision,
                    "TEST_BRANCH_CREATED": str(temporary_path / "branch-created"),
                    "TEST_GRAPHQL_PAYLOAD": str(graphql_payload),
                    "TEST_GH_LOG": str(gh_log),
                    "TEST_PR_CREATED": str(temporary_path / "pr-created"),
                }
            )
            subprocess.run(
                [str(repository_root / "actions/update-infrastructure-consumer/publish")],
                cwd=repository_root,
                check=True,
                env=environment,
            )

            payload = json.loads(graphql_payload.read_text(encoding="utf-8"))
            mutation_input = payload["variables"]["input"]
            self.assertEqual(mutation_input["expectedHeadOid"], base_revision)
            self.assertEqual(
                mutation_input["branch"]["branchName"],
                f"maintenance/infrastructure/{source_revision}",
            )
            additions = {
                item["path"]: base64.b64decode(item["contents"]).decode()
                for item in mutation_input["fileChanges"]["additions"]
            }
            self.assertEqual(additions["ci/aws/infrastructure-revision"].strip(), source_revision)
            self.assertEqual(
                additions["ci/aws/infrastructure-contract-version"].strip(), contract_version
            )
            self.assertIn(source_revision, additions[".github/workflows/ci.yml"])
            self.assertIn("pull_request_number=17", output.read_text(encoding="utf-8"))
            calls = gh_log.read_text(encoding="utf-8")
            self.assertIn("pr\tcreate", calls)
            self.assertIn("pr\tmerge\t17", calls)
            self.assertIn("--auto", calls)
            self.assertIn("api\tgraphql", calls)
            self.assertIn("--match-head-commit\t" + "a" * 40, calls)
            self.assertNotIn("test-token", calls)


if __name__ == "__main__":
    unittest.main()
