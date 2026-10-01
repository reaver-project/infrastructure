import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import lib  # noqa: E402

repository = "reaver-project/reaveros"
sha = "0123456789abcdef0123456789abcdef01234567"
allowed = {repository}


def job_event(action="queued", branch="main", key="unit-tests-amd64"):
    return {
        "action": action,
        "repository": {"full_name": repository, "id": 42},
        "installation": {"id": 17},
        "workflow_job": {
            "id": 123,
            "run_id": 456,
            "run_attempt": 2,
            "head_branch": branch,
            "head_sha": sha,
            "name": "Unit tests (amd64) / Run unit-tests amd64",
            "status": action,
            "labels": ["self-hosted", "reaveros-aws", f"reaveros-456-2-{key}"],
        },
    }


class RunnerWebhookValidationTests(unittest.TestCase):
    def test_routes_queued_main_job_to_trusted_controller(self):
        trust, task = lib.normalize_job_event(job_event(), allowed)
        self.assertEqual(trust, "trusted")
        self.assertEqual(task["job_id"], 123)
        self.assertEqual(task["run_id"], 456)
        self.assertEqual(task["run_attempt"], 2)
        self.assertEqual(task["runner_key"], "unit-tests-amd64")
        self.assertEqual(task["runner_name"], "reaveros-456-2-unit-tests-amd64")
        waiting = job_event()
        waiting["workflow_job"]["status"] = "waiting"
        self.assertEqual(lib.normalize_job_event(waiting, allowed)[1]["action"], "queued")

    def test_routes_copied_pr_job_to_candidate_controller(self):
        trust, task = lib.normalize_job_event(job_event(branch="pull-request/247"), allowed)
        self.assertEqual(trust, "candidate")
        self.assertEqual(task["head_branch"], "pull-request/247")

    def test_accepts_completion_for_the_same_job(self):
        trust, task = lib.normalize_job_event(job_event(action="completed"), allowed)
        self.assertEqual(trust, "trusted")
        self.assertEqual(task["action"], "completed")

    def test_ignores_other_events_and_repositories(self):
        self.assertIsNone(lib.normalize_job_event(job_event(action="in_progress"), allowed))
        other_repository = job_event()
        other_repository["repository"]["full_name"] = "reaver-project/infrastructure"
        self.assertIsNone(lib.normalize_job_event(other_repository, allowed))
        other_runner = job_event()
        other_runner["workflow_job"]["labels"] = ["ubuntu-latest"]
        self.assertIsNone(lib.normalize_job_event(other_runner, allowed))

    def test_rejects_unadmitted_branches_and_mismatched_status(self):
        with self.assertRaisesRegex(ValueError, "not admitted"):
            lib.normalize_job_event(job_event(branch="feature/unreviewed"), allowed)
        event = job_event()
        event["workflow_job"]["status"] = "completed"
        with self.assertRaisesRegex(ValueError, "does not match"):
            lib.normalize_job_event(event, allowed)

    def test_rejects_unexpected_labels_and_runner_identity(self):
        event = job_event()
        event["workflow_job"]["labels"].append("trusted")
        with self.assertRaisesRegex(ValueError, "unexpected runner labels"):
            lib.normalize_job_event(event, allowed)
        event = job_event()
        event["workflow_job"]["labels"][2] = "reaveros-456-3-unit-tests-amd64"
        with self.assertRaisesRegex(ValueError, "exactly one runner identity"):
            lib.normalize_job_event(event, allowed)

    def test_rejects_malformed_ids_and_sha(self):
        for field in ("id", "run_id", "run_attempt"):
            event = job_event()
            event["workflow_job"][field] = True
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "invalid"):
                lib.normalize_job_event(event, allowed)
        event = job_event()
        event["workflow_job"]["head_sha"] = "bad"
        with self.assertRaisesRegex(ValueError, "SHA is invalid"):
            lib.normalize_job_event(event, allowed)

    def test_requires_app_installation_and_repository_identity(self):
        event = job_event()
        event.pop("installation")
        with self.assertRaisesRegex(ValueError, "installation is missing"):
            lib.normalize_job_event(event, allowed)
        event = job_event()
        event["repository"]["id"] = 0
        with self.assertRaisesRegex(ValueError, "repository ID"):
            lib.normalize_job_event(event, allowed)

    def test_rejects_missing_or_malformed_job_fields(self):
        cases = (
            (lambda event: event.pop("repository"), "repository is missing"),
            (
                lambda event: event["repository"].update(full_name="invalid/name/extra"),
                "repository name is invalid",
            ),
            (lambda event: event.pop("workflow_job"), "workflow job is missing"),
            (
                lambda event: event["workflow_job"].update(labels="reaveros-aws"),
                "workflow job labels are invalid",
            ),
            (
                lambda event: event["workflow_job"].update(name=""),
                "workflow job name is invalid",
            ),
        )
        for corrupt, message in cases:
            event = job_event()
            corrupt(event)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                lib.normalize_job_event(event, allowed)


if __name__ == "__main__":
    unittest.main()
