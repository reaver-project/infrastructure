import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

old_revision = "1" * 40


class UpdateInfrastructureConsumerTests(unittest.TestCase):
    def test_publishes_and_enables_auto_merge_for_a_contract_update(self):
        repository_root = Path(__file__).resolve().parents[1]
        source_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

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
            subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=source,
                check=True,
            )
            subprocess.run(["git", "add", "--all"], cwd=source, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "--no-gpg-sign", "-m", "Base"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "clone", "--quiet", "--bare", str(source), str(remote)],
                check=True,
            )

            mock_directory = temporary_path / "bin"
            mock_directory.mkdir()
            mock_gh = mock_directory / "gh"
            mock_gh.write_text(
                """#!/usr/bin/env python3
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
            environment = os.environ.copy()
            environment.update(
                {
                    "APP_SLUG": "reaver-project-maintenance",
                    "CONSUMER_REPOSITORY": "reaver-project/reaveros",
                    "CONTRACT_VERSION": "1",
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

            branch = f"maintenance/infrastructure/{source_revision}"
            published_revision = subprocess.run(
                [
                    "git",
                    f"--git-dir={remote}",
                    "show",
                    f"{branch}:ci/aws/infrastructure-revision",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(published_revision, source_revision)
            self.assertIn("pull_request_number=17", output.read_text(encoding="utf-8"))
            calls = gh_log.read_text(encoding="utf-8")
            self.assertIn("pr\tcreate", calls)
            self.assertIn("pr\tmerge\t17", calls)
            self.assertIn("--auto", calls)
            self.assertNotIn("test-token", calls)


if __name__ == "__main__":
    unittest.main()
