import base64
import datetime
import importlib.util
import os
import pathlib
import sys
import types
import unittest
from unittest import mock


class BotoCoreError(Exception):
    pass


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


clients = {
    "ec2": mock.Mock(),
    "secretsmanager": mock.Mock(),
    "ssm": mock.Mock(),
}
boto3 = types.ModuleType("boto3")
boto3.client = lambda name: clients[name]
botocore = types.ModuleType("botocore")
botocore_exceptions = types.ModuleType("botocore.exceptions")
botocore_exceptions.BotoCoreError = BotoCoreError
botocore_exceptions.ClientError = ClientError
botocore.exceptions = botocore_exceptions
primitives = types.ModuleType("cryptography.hazmat.primitives")
primitives.hashes = mock.Mock()
primitives.serialization = mock.Mock()
asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
asymmetric.padding = mock.Mock()

sys.modules.update({
    "boto3": boto3,
    "botocore": botocore,
    "botocore.exceptions": botocore_exceptions,
    "cryptography": types.ModuleType("cryptography"),
    "cryptography.hazmat": types.ModuleType("cryptography.hazmat"),
    "cryptography.hazmat.primitives": primitives,
    "cryptography.hazmat.primitives.asymmetric": asymmetric,
})

module_directory = pathlib.Path(__file__).parent
lib_specification = importlib.util.spec_from_file_location(
    "lib",
    module_directory / "lib.py",
)
lib = importlib.util.module_from_spec(lib_specification)
lib_specification.loader.exec_module(lib)
sys.modules["lib"] = lib
index_specification = importlib.util.spec_from_file_location(
    "runner_control_index",
    module_directory / "index.py",
)
runner_control = importlib.util.module_from_spec(index_specification)
index_specification.loader.exec_module(runner_control)


class RunnerControlIndexTests(unittest.TestCase):
    def setUp(self):
        for client in clients.values():
            client.reset_mock()
        clients["ssm"].delete_parameter.side_effect = None
        clients["ssm"].put_parameter.side_effect = None
        self.environment = mock.patch.dict(os.environ, {
            "ALLOWED_REPOSITORIES": "reaver-project/reaveros",
            "BUILDER_PROFILE_ARN": "arn:builder",
            "GITHUB_ORGANIZATION": "reaver-project",
            "GITHUB_RUNNER_GROUP_ID": "7",
            "JIT_PARAMETER_PREFIX": "/jit/",
            "LARGE_INSTANCE_TYPE": "c8i.8xlarge",
            "LAUNCH_TEMPLATE_ID": "lt-123",
            "MAXIMUM_CONCURRENT_RUNNERS": "4",
            "MAXIMUM_AGE_MINUTES": "180",
            "MEDIUM_INSTANCE_TYPE": "c8i.4xlarge",
            "RUNNER_INSTANCE_NAME": "reaveros-runner",
            "VALIDATION_PROFILE_ARN": "arn:validation",
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_terminate_uses_instance_tags_for_registration_cleanup(self):
        clients["ec2"].describe_instances.return_value = {
            "Reservations": [{"Instances": [{
                "InstanceId": "i-123abc",
                "Tags": [
                    {"Key": "GitHubRunnerId", "Value": "42"},
                    {"Key": "ReaverOSRunnerParameter", "Value": "/jit/real"},
                ],
            }]}],
        }
        clients["ec2"].get_console_output.return_value = {
            "Output": base64.b64encode(b"runner output").decode(),
        }
        with mock.patch.object(runner_control, "github_token", return_value="token"), \
            mock.patch.object(runner_control, "github_request") as github_request:
            result = runner_control.terminate({
                "instance_id": "i-123abc",
                "jit_parameter": "/jit/untrusted",
                "runner_id": 99,
            })

        self.assertEqual(result["console_output"], "runner output")
        clients["ssm"].delete_parameter.assert_called_once_with(Name="/jit/real")
        github_request.assert_called_once_with(
            "/orgs/reaver-project/actions/runners/42",
            "token",
            "DELETE",
        )
        clients["ec2"].terminate_instances.assert_called_once_with(
            InstanceIds=["i-123abc"],
        )

    def test_reap_cleans_registration_before_terminating(self):
        instance = {
            "InstanceId": "i-123abc",
            "LaunchTime": datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
            "State": {"Name": "running"},
            "Tags": [
                {"Key": "GitHubRunnerId", "Value": "42"},
                {"Key": "ReaverOSRunnerParameter", "Value": "/jit/real"},
            ],
        }
        with mock.patch.object(runner_control, "runner_instances", return_value=[instance]), \
            mock.patch.object(runner_control, "cleanup_registration") as cleanup:
            result = runner_control.reap({})

        cleanup.assert_called_once_with("/jit/real", 42)
        clients["ec2"].terminate_instances.assert_called_once_with(
            InstanceIds=["i-123abc"],
        )
        self.assertEqual(result, {"terminated": ["i-123abc"]})

    def test_cleanup_attempts_github_after_an_ssm_failure(self):
        clients["ssm"].delete_parameter.side_effect = ClientError("AccessDenied")
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request") as github_request,
            self.assertRaisesRegex(RuntimeError, "registration cleanup failed"),
        ):
            runner_control.cleanup_registration("/jit/real", 42)

        github_request.assert_called_once()

    def test_launch_rolls_back_registration_after_an_ssm_failure(self):
        clients["ssm"].put_parameter.side_effect = ClientError("AccessDenied")
        jit = {
            "encoded_jit_config": "encoded",
            "runner": {"id": 42},
        }
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request", return_value=jit),
            mock.patch.object(runner_control, "cleanup_registration") as cleanup,
            mock.patch.object(
                lib.secrets,
                "token_hex",
                return_value="0123456789abcdef0123456789abcdef",
            ),
            self.assertRaises(ClientError),
        ):
            runner_control.launch({
                "github_run_attempt": 2,
                "github_run_id": 123,
                "repository": "reaver-project/reaveros",
                "runner_key": "unit-tests-amd64",
                "runner_profile": "validation",
                "runner_size": "medium",
            })

        cleanup.assert_called_once_with(
            "/jit/123-2-unit-tests-amd64-0123456789abcdef0123456789abcdef",
            42,
        )
        clients["ec2"].run_instances.assert_not_called()


if __name__ == "__main__":
    unittest.main()
