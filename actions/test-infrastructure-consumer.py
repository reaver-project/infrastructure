import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

module_path = Path(__file__).with_name("lib") / "infrastructure_consumer.py"
specification = importlib.util.spec_from_file_location(
    "infrastructure_consumer", module_path
)
infrastructure_consumer = importlib.util.module_from_spec(specification)
specification.loader.exec_module(infrastructure_consumer)


old_revision = "1" * 40
new_revision = "2" * 40


class InfrastructureConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / "ci" / "aws").mkdir(parents=True)
        (self.root / "ci" / "aws" / "infrastructure-revision").write_text(
            f"{old_revision}\n", encoding="utf-8"
        )
        (self.root / "ci" / "aws" / "infrastructure-contract-version").write_text(
            "1\n", encoding="utf-8"
        )
        (self.root / ".github" / "workflows" / "ci.yml").write_text(
            f"""name: CI
jobs:
    contract:
        uses: reaver-project/infrastructure/actions/aws-stack-contract@{old_revision}
        with:
            expected-contract-version: '1'
""",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.root, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "--no-gpg-sign", "-m", "Base"],
            cwd=self.root,
            check=True,
        )
        self.base_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def rewrite_and_commit(self):
        changed_files = infrastructure_consumer.rewrite_consumer(
            self.root,
            "ci/aws/infrastructure-revision",
            "ci/aws/infrastructure-contract-version",
            new_revision,
            "2",
        )
        self.assertEqual(
            changed_files,
            [
                ".github/workflows/ci.yml",
                "ci/aws/infrastructure-contract-version",
                "ci/aws/infrastructure-revision",
            ],
        )
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "--no-gpg-sign", "-m", "Update"],
            cwd=self.root,
            check=True,
        )

    def test_rewrites_and_strictly_validates_only_contract_changes(self):
        self.rewrite_and_commit()
        state = infrastructure_consumer.read_state(
            self.root,
            "ci/aws/infrastructure-revision",
            "ci/aws/infrastructure-contract-version",
        )
        infrastructure_consumer.require_expected_state(state, new_revision, "2")
        infrastructure_consumer.validate_strict_update(
            self.root,
            self.base_revision,
            state,
            "ci/aws/infrastructure-revision",
            "ci/aws/infrastructure-contract-version",
        )

    def test_rejects_an_unrelated_change_in_an_automatic_update(self):
        self.rewrite_and_commit()
        (self.root / "README.md").write_text("unrelated\n", encoding="utf-8")
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "--no-gpg-sign", "-m", "Unrelated"],
            cwd=self.root,
            check=True,
        )
        state = infrastructure_consumer.read_state(
            self.root,
            "ci/aws/infrastructure-revision",
            "ci/aws/infrastructure-contract-version",
        )
        with self.assertRaisesRegex(ValueError, "unrelated files"):
            infrastructure_consumer.validate_strict_update(
                self.root,
                self.base_revision,
                state,
                "ci/aws/infrastructure-revision",
                "ci/aws/infrastructure-contract-version",
            )

    def test_rejects_non_contract_workflow_edits(self):
        self.rewrite_and_commit()
        workflow = self.root / ".github" / "workflows" / "ci.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8") + "# unexpected\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "--all"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "--no-gpg-sign", "-m", "Unexpected"],
            cwd=self.root,
            check=True,
        )
        state = infrastructure_consumer.read_state(
            self.root,
            "ci/aws/infrastructure-revision",
            "ci/aws/infrastructure-contract-version",
        )
        with self.assertRaisesRegex(ValueError, "non-contract changes"):
            infrastructure_consumer.validate_strict_update(
                self.root,
                self.base_revision,
                state,
                "ci/aws/infrastructure-revision",
                "ci/aws/infrastructure-contract-version",
            )

    def test_rejects_a_mutable_shared_action_reference(self):
        workflow = self.root / ".github" / "workflows" / "ci.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                f"@{old_revision}", "@main"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "not pinned by full Git SHA"):
            infrastructure_consumer.read_state(
                self.root,
                "ci/aws/infrastructure-revision",
                "ci/aws/infrastructure-contract-version",
            )

    def test_rejects_a_non_literal_contract_input(self):
        workflow = self.root / ".github" / "workflows" / "ci.yml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace(
                "expected-contract-version: '1'",
                "expected-contract-version: latest",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "not a literal version"):
            infrastructure_consumer.read_state(
                self.root,
                "ci/aws/infrastructure-revision",
                "ci/aws/infrastructure-contract-version",
            )


if __name__ == "__main__":
    unittest.main()
