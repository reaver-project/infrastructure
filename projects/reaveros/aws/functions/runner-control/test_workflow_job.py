import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from workflow_job import runner_specification, validate_fetched_job


class WorkflowJobTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "runner_key": "prepare-medium",
            "job_name": "Prepare build environment (medium) / Run AWS prepare",
            "runner_class": None,
            "job_id": 3,
            "run_id": 2,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "runner_name": "reaveros-2-1-prepare-medium",
            "runner_group_id": 7,
            "repository": "reaver-project/reaveros",
            "repository_id": 5,
        }
        self.fetched = {
            "id": 3,
            "run_id": 2,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "name": self.job["job_name"],
            "workflow_name": "CI",
            "labels": ["self-hosted", "reaveros-aws", "reaveros-2-1-prepare-medium"],
            "runner_group_id": None,
        }
        self.run = {
            "id": 2,
            "run_attempt": 1,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "repository": {"full_name": "reaver-project/reaveros", "id": 5},
            "event": "push",
        }

    def test_accepts_existing_jobs_and_generic_resource_classes(self):
        self.assertEqual(
            validate_fetched_job(self.job, self.fetched, self.run, "trusted"),
            {"runner_size": "medium", "runner_profile": "builder", "source_ref": "refs/heads/main"},
        )
        self.assertEqual(
            validate_fetched_job(
                self.job, {**self.fetched, "workflow_name": None}, self.run, "trusted"
            )["source_ref"],
            "refs/heads/main",
        )
        large = {
            **self.job,
            "runner_key": "prepare-large",
            "job_name": "Rebuild build environment (large) / Run AWS prepare",
        }
        self.assertEqual(runner_specification(large)["runner_size"], "large")
        self.assertEqual(
            runner_specification({**self.job, "runner_key": "new-test-riscv"}),
            {"runner_size": "medium", "runner_profile": "validation"},
        )
        for profile in ("builder", "validation"):
            for size in ("medium", "large"):
                with self.subTest(profile=profile, size=size):
                    self.assertEqual(
                        runner_specification({**self.job, "runner_class": f"{profile}-{size}"}),
                        {"runner_size": size, "runner_profile": profile},
                    )
        with self.assertRaisesRegex(ValueError, "unsupported runner class"):
            runner_specification({**self.job, "runner_class": "administrator-huge"})

    def test_display_names_are_not_runner_policy(self):
        job = {**self.job, "job_name": "Prepare / Build environment (medium)"}
        fetched = {
            **self.fetched,
            "name": job["job_name"],
            "workflow_name": "renamed CI workflow",
        }
        run = {**self.run, "name": "renamed CI workflow"}
        self.assertEqual(
            validate_fetched_job(job, fetched, run, "trusted")["runner_profile"],
            "builder",
        )

    def test_class_label_selects_resource_profile(self):
        job = {**self.job, "runner_class": "validation-large"}
        fetched = {
            **self.fetched,
            "labels": [*self.fetched["labels"], "reaveros-class-validation-large"],
        }
        self.assertEqual(
            validate_fetched_job(job, fetched, self.run, "trusted")["runner_size"],
            "large",
        )
        with self.assertRaisesRegex(ValueError, "unexpected labels"):
            validate_fetched_job(
                job,
                {**fetched, "labels": self.fetched["labels"]},
                self.run,
                "trusted",
            )

    def test_allows_only_canceled_unassigned_jobs_without_a_runner_group(self):
        canceled = {
            **self.fetched,
            "status": "completed",
            "conclusion": "cancelled",
            "runner_id": 0,
            "runner_group_id": 0,
        }
        self.assertEqual(
            validate_fetched_job(self.job, canceled, self.run, "trusted")["runner_profile"],
            "builder",
        )
        for change in (
            {"status": "queued"},
            {"conclusion": "success"},
            {"runner_id": 9},
            {"runner_group_id": 99},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(ValueError, "unexpected runner group"),
            ):
                validate_fetched_job(self.job, {**canceled, **change}, self.run, "trusted")

    def test_rejects_metadata_changes(self):
        for name, value in (
            ("id", 4),
            ("run_id", 4),
            ("run_attempt", 2),
            ("head_sha", "b" * 40),
            ("head_branch", "other"),
            ("name", "other"),
            ("labels", ["self-hosted"]),
            ("runner_group_id", 99),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_fetched_job(self.job, {**self.fetched, name: value}, self.run, "trusted")
        for name, value in (
            ("id", 4),
            ("run_attempt", 2),
            ("head_sha", "b" * 40),
            ("head_branch", "other"),
            ("path", "other"),
            ("event", "pull_request"),
            ("repository", {"full_name": "other/repo", "id": 5}),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_fetched_job(self.job, self.fetched, {**self.run, name: value}, "trusted")
        with self.assertRaisesRegex(ValueError, "untrusted branch"):
            validate_fetched_job(self.job, self.fetched, self.run, "candidate")
        with self.assertRaisesRegex(ValueError, "invalid workflow job metadata"):
            validate_fetched_job(self.job, None, self.run, "trusted")
        with self.assertRaisesRegex(ValueError, "untrusted branch"):
            validate_fetched_job(
                {**self.job, "head_branch": "pull-request/17"},
                self.fetched,
                self.run,
                "trusted",
            )


if __name__ == "__main__":
    unittest.main()
