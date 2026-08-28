import datetime
import importlib.util
import pathlib
import unittest

module_path = pathlib.Path(__file__).with_name("lib.py")
specification = importlib.util.spec_from_file_location("runner_control_lib", module_path)
runner_control = importlib.util.module_from_spec(specification)
specification.loader.exec_module(runner_control)


class RunnerControlTests(unittest.TestCase):
    def test_validates_repository_allowlist(self):
        runner_control.validate_repository(
            "reaver-project/reaveros",
            {"reaver-project/reaveros"},
        )
        with self.assertRaises(ValueError):
            runner_control.validate_repository(
                "somewhere/else",
                {"reaver-project/reaveros"},
            )

    def test_builds_unique_runner_identity(self):
        self.assertEqual(
            runner_control.runner_identity(
                {
                    "github_run_attempt": 2,
                    "github_run_id": 123,
                    "runner_key": "unit-tests-amd64",
                },
                "/jit/",
                "0123456789abcdef0123456789abcdef",
            ),
            {
                "jit_parameter": ("/jit/123-2-unit-tests-amd64-0123456789abcdef0123456789abcdef"),
                "run_id": "123",
                "runner_name": "reaveros-123-2-unit-tests-amd64",
            },
        )

    def test_owns_instance_and_volume_tags_at_launch(self):
        self.assertEqual(
            runner_control.runner_tag_specifications(
                {
                    "jit_parameter": "/jit/123-2-unit-tests-amd64",
                    "run_id": "123",
                    "runner_id": 42,
                    "runner_name": "reaveros-123-2-unit-tests-amd64",
                },
                "reaver-project/reaveros",
                "medium",
                "validation",
                "reaveros-github-runners-runner",
            ),
            [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": "reaveros-github-runners-runner",
                        },
                        {"Key": "Project", "Value": "ReaverOS"},
                        {
                            "Key": "ReaverOSPurpose",
                            "Value": "GitHubActionsRunner",
                        },
                        {
                            "Key": "GitHubRepository",
                            "Value": "reaver-project/reaveros",
                        },
                        {"Key": "GitHubRunId", "Value": "123"},
                        {"Key": "GitHubRunnerId", "Value": "42"},
                        {
                            "Key": "GitHubRunnerName",
                            "Value": "reaveros-123-2-unit-tests-amd64",
                        },
                        {
                            "Key": "ReaverOSRunnerParameter",
                            "Value": "/jit/123-2-unit-tests-amd64",
                        },
                        {"Key": "ReaverOSRunnerProfile", "Value": "validation"},
                        {"Key": "ReaverOSRunnerSize", "Value": "medium"},
                    ],
                },
                {
                    "ResourceType": "volume",
                    "Tags": [
                        {"Key": "Project", "Value": "ReaverOS"},
                        {
                            "Key": "ReaverOSPurpose",
                            "Value": "GitHubActionsRunner",
                        },
                    ],
                },
            ],
        )

    def test_selects_complete_cleanup_for_expired_runners(self):
        now = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)
        self.assertEqual(
            runner_control.expired_runner_cleanup(
                [
                    {
                        "InstanceId": "i-old",
                        "LaunchTime": now - datetime.timedelta(minutes=181),
                        "Tags": [
                            {"Key": "GitHubRunnerId", "Value": "42"},
                            {
                                "Key": "ReaverOSRunnerParameter",
                                "Value": "/jit/123-1-tests",
                            },
                        ],
                    },
                    {
                        "InstanceId": "i-live",
                        "LaunchTime": now - datetime.timedelta(minutes=179),
                    },
                ],
                now,
                180,
                "/jit/",
            ),
            [
                {
                    "instance_id": "i-old",
                    "jit_parameter": "/jit/123-1-tests",
                    "runner_id": 42,
                }
            ],
        )

    def test_ignores_untrusted_cleanup_tags(self):
        now = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)
        self.assertEqual(
            runner_control.expired_runner_cleanup(
                [
                    {
                        "InstanceId": "i-old",
                        "LaunchTime": now - datetime.timedelta(minutes=181),
                        "Tags": [
                            {"Key": "GitHubRunnerId", "Value": "not-a-number"},
                            {"Key": "ReaverOSRunnerParameter", "Value": "/other/value"},
                        ],
                    }
                ],
                now,
                180,
                "/jit/",
            ),
            [
                {
                    "instance_id": "i-old",
                    "jit_parameter": None,
                    "runner_id": None,
                }
            ],
        )

    def test_derives_normal_cleanup_from_instance_tags(self):
        self.assertEqual(
            runner_control.runner_cleanup(
                {
                    "InstanceId": "i-runner",
                    "Tags": [
                        {"Key": "GitHubRunnerId", "Value": "42"},
                        {
                            "Key": "ReaverOSRunnerParameter",
                            "Value": "/jit/123-1-tests",
                        },
                    ],
                },
                "/jit/",
            ),
            {
                "instance_id": "i-runner",
                "jit_parameter": "/jit/123-1-tests",
                "runner_id": 42,
            },
        )


if __name__ == "__main__":
    unittest.main()
