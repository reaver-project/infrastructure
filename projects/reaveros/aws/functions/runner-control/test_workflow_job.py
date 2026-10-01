import unittest

from workflow_job import expected_job, validate_fetched_job


class WorkflowJobTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "runner_key": "prepare-medium",
            "job_name": "Prepare build environment (medium) / Run AWS prepare",
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

    def test_accepts_only_known_jobs(self):
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
        self.assertEqual(expected_job(large)["runner_size"], "large")
        for task, name, target in (
            ("build-dependencies", "Check build-system dependencies", "amd64"),
            ("unit-tests", "Unit tests", "amd64"),
            ("image", "Build image", "uefi-efipart-amd64"),
            ("boot", "Boot smoke test", "uefi-efipart-amd64"),
        ):
            job = {
                **self.job,
                "runner_key": f"{task}-{target}",
                "job_name": f"{name} ({target}) / Run AWS {task} {target}",
            }
            self.assertEqual(expected_job(job)["runner_profile"], "validation")
        with self.assertRaisesRegex(ValueError, "unexpected runner job"):
            expected_job({**self.job, "runner_key": "arbitrary"})
        with self.assertRaisesRegex(ValueError, "unexpected preparation job"):
            expected_job(
                {**self.job, "job_name": "Prepare build environment (medium) / Run prepare"}
            )
        with self.assertRaisesRegex(ValueError, "unexpected preparation job"):
            expected_job({**large, "runner_key": "prepare-medium"})
        with self.assertRaisesRegex(ValueError, "unexpected validation job"):
            expected_job(
                {**self.job, "runner_key": "unit-tests-amd64", "job_name": "Unrecognized job"}
            )

    def test_rejects_metadata_changes(self):
        for name, value in (
            ("id", 4),
            ("run_id", 4),
            ("run_attempt", 2),
            ("head_sha", "b" * 40),
            ("head_branch", "other"),
            ("name", "other"),
            ("workflow_name", "other"),
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
            ("name", "other"),
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
