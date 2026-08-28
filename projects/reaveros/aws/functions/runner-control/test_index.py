import base64
import datetime
import importlib.util
import io
import json
import os
import pathlib
import sys
import types
import unittest
import urllib.error
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
            client.reset_mock(return_value=True, side_effect=True)
        primitives.hashes.reset_mock(return_value=True, side_effect=True)
        primitives.serialization.reset_mock(return_value=True, side_effect=True)
        asymmetric.padding.reset_mock(return_value=True, side_effect=True)
        runner_control.cached_github_token = None
        runner_control.cached_github_token_expiry = 0
        self.environment = mock.patch.dict(os.environ, {
            "ALLOWED_REPOSITORIES": "reaver-project/reaveros",
            "BUILDER_PROFILE_ARN": "arn:builder",
            "GITHUB_ORGANIZATION": "reaver-project",
            "GITHUB_RUNNER_GROUP_ID": "7",
            "GITHUB_APP_SECRET_ID": "runner-app-secret",
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

    def test_creates_a_signed_app_jwt(self):
        private_key = mock.Mock()
        private_key.sign.return_value = b"signature"
        primitives.serialization.load_pem_private_key.return_value = private_key

        token = runner_control.create_app_jwt(
            {"app_id": 1234, "private_key": "test-private-key"},
            now=1000,
        )

        header, payload, signature = token.split(".")
        def decode(value):
            return json.loads(base64.urlsafe_b64decode(
                value + "=" * (-len(value) % 4)
            ))

        self.assertEqual(decode(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            decode(payload),
            {"exp": 1540, "iat": 940, "iss": 1234},
        )
        self.assertEqual(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            b"signature",
        )
        primitives.serialization.load_pem_private_key.assert_called_once_with(
            b"test-private-key",
            password=None,
        )
        private_key.sign.assert_called_once()

    def test_github_request_handles_json_empty_and_error_responses(self):
        json_response = mock.MagicMock()
        json_response.status = 200
        json_response.read.return_value = b'{"answer":42}'
        empty_response = mock.MagicMock()
        empty_response.status = 204
        error = urllib.error.HTTPError(
            "https://api.github.com/test",
            422,
            "unprocessable",
            {},
            io.BytesIO(b"invalid request"),
        )

        with mock.patch.object(
            runner_control.urllib.request,
            "urlopen",
            side_effect=[
                mock.MagicMock(__enter__=mock.Mock(return_value=json_response)),
                mock.MagicMock(__enter__=mock.Mock(return_value=empty_response)),
                error,
            ],
        ) as urlopen:
            self.assertEqual(
                runner_control.github_request(
                    "/test",
                    "token",
                    "POST",
                    {"value": True},
                ),
                {"answer": 42},
            )
            self.assertIsNone(runner_control.github_request("/empty", "token"))
            with self.assertRaisesRegex(
                runner_control.GitHubRequestError,
                "422 invalid request",
            ) as raised:
                runner_control.github_request("/test", "token")

        self.assertEqual(raised.exception.status, 422)
        request = urlopen.call_args_list[0].args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"value": True})
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(
            request.get_header("X-github-api-version"),
            "2026-03-10",
        )

    def test_github_request_retries_transient_failures(self):
        rate_limit = urllib.error.HTTPError(
            "https://api.github.com/test",
            429,
            "rate limited",
            {"Retry-After": "2.5"},
            io.BytesIO(b"rate limited"),
        )
        server_error = urllib.error.HTTPError(
            "https://api.github.com/test",
            503,
            "unavailable",
            {},
            io.BytesIO(b"unavailable"),
        )
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"answer":42}'

        with (
            mock.patch.object(
                runner_control.urllib.request,
                "urlopen",
                side_effect=[
                    rate_limit,
                    server_error,
                    mock.MagicMock(__enter__=mock.Mock(return_value=response)),
                ],
            ),
            mock.patch.object(runner_control.time, "sleep") as sleep,
        ):
            self.assertEqual(
                runner_control.github_request("/test", "token"),
                {"answer": 42},
            )

        self.assertEqual(sleep.call_args_list, [mock.call(2.5), mock.call(2)])

    def test_github_request_limits_retries(self):
        failures = [
            urllib.error.URLError("temporary failure"),
            urllib.error.URLError("temporary failure"),
            urllib.error.URLError("permanent failure"),
        ]
        with (
            mock.patch.object(
                runner_control.urllib.request,
                "urlopen",
                side_effect=failures,
            ) as urlopen,
            mock.patch.object(runner_control.time, "sleep") as sleep,
            self.assertRaisesRegex(
                runner_control.GitHubRequestError,
                "network error permanent failure",
            ),
        ):
            runner_control.github_request("/test", "token")

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_github_request_does_not_retry_client_errors(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/test",
            422,
            "unprocessable",
            {},
            io.BytesIO(b"invalid request"),
        )
        with (
            mock.patch.object(
                runner_control.urllib.request,
                "urlopen",
                side_effect=error,
            ) as urlopen,
            mock.patch.object(runner_control.time, "sleep") as sleep,
            self.assertRaises(runner_control.GitHubRequestError),
        ):
            runner_control.github_request("/test", "token")

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_github_token_is_created_and_cached(self):
        clients["secretsmanager"].get_secret_value.return_value = {
            "SecretString": json.dumps({
                "app_id": 1234,
                "installation_id": 5678,
                "private_key": "test-private-key",
            }),
        }
        installation_token = {
            "expires_at": "2030-01-01T00:00:00Z",
            "token": "installation-token",
        }

        with (
            mock.patch.object(
                runner_control,
                "create_app_jwt",
                return_value="app-jwt",
            ) as create_app_jwt,
            mock.patch.object(
                runner_control,
                "github_request",
                return_value=installation_token,
            ) as github_request,
            mock.patch.object(runner_control.time, "time", return_value=1000),
        ):
            self.assertEqual(runner_control.github_token(), "installation-token")
            self.assertEqual(runner_control.github_token(), "installation-token")

        clients["secretsmanager"].get_secret_value.assert_called_once_with(
            SecretId="runner-app-secret",
        )
        create_app_jwt.assert_called_once()
        github_request.assert_called_once_with(
            "/app/installations/5678/access_tokens",
            "app-jwt",
            "POST",
            {},
        )

    def test_runner_instance_lookup_follows_pagination(self):
        first = {"InstanceId": "i-first"}
        second = {"InstanceId": "i-second"}
        clients["ec2"].describe_instances.side_effect = [
            {
                "NextToken": "next-page",
                "Reservations": [{"Instances": [first]}],
            },
            {"Reservations": [{"Instances": [second]}]},
        ]

        self.assertEqual(
            runner_control.runner_instances(["i-first", "i-second"]),
            [first, second],
        )
        filters = [{
            "Name": "tag:ReaverOSPurpose",
            "Values": ["GitHubActionsRunner"],
        }]
        self.assertEqual(
            clients["ec2"].describe_instances.call_args_list,
            [
                mock.call(
                    Filters=filters,
                    InstanceIds=["i-first", "i-second"],
                ),
                mock.call(
                    Filters=filters,
                    InstanceIds=["i-first", "i-second"],
                    NextToken="next-page",
                ),
            ],
        )

    def test_jit_parameter_expiration_outlives_the_runner(self):
        policy = json.loads(
            runner_control.parameter_expiration_policy(
                180,
                datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC),
            )
        )

        self.assertEqual(
            policy,
            [{
                "Type": "Expiration",
                "Version": "1.0",
                "Attributes": {"Timestamp": "2030-01-01T04:00:00Z"},
            }],
        )

    def test_launches_a_valid_runner(self):
        clients["ec2"].run_instances.return_value = {
            "Instances": [{"InstanceId": "i-launched"}],
        }
        jit = {
            "encoded_jit_config": "encoded",
            "runner": {"id": 42},
        }
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                return_value=jit,
            ) as github_request,
            mock.patch.object(
                lib.secrets,
                "token_hex",
                return_value="0123456789abcdef0123456789abcdef",
            ),
        ):
            result = runner_control.launch({
                "github_run_attempt": 2,
                "github_run_id": 123,
                "repository": "reaver-project/reaveros",
                "runner_key": "unit-tests-amd64",
                "runner_profile": "validation",
                "runner_size": "medium",
            })

        self.assertEqual(result, {
            "instance_id": "i-launched",
            "labels": [
                "self-hosted",
                "reaveros-aws",
                "reaveros-123-2-unit-tests-amd64",
            ],
            "runner_name": "reaveros-123-2-unit-tests-amd64",
        })
        github_request.assert_called_once()
        put_parameter = clients["ssm"].put_parameter.call_args.kwargs
        self.assertEqual(put_parameter["Tier"], "Advanced")
        self.assertEqual(
            json.loads(put_parameter["Policies"])[0]["Type"],
            "Expiration",
        )
        clients["ec2"].run_instances.assert_called_once()

    def test_launch_rejects_invalid_profiles_sizes_and_capacity(self):
        base_event = {
            "github_run_attempt": 2,
            "github_run_id": 123,
            "repository": "reaver-project/reaveros",
            "runner_key": "unit-tests-amd64",
            "runner_profile": "validation",
            "runner_size": "medium",
        }
        for field, invalid, message in (
            ("runner_size", "enormous", "invalid runner size"),
            ("runner_profile", "administrator", "invalid runner profile"),
        ):
            event = {**base_event, field: invalid}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                runner_control.launch(event)

        live_runner = {"State": {"Name": "running"}}
        with (
            mock.patch.object(
                runner_control,
                "runner_instances",
                return_value=[live_runner] * 4,
            ),
            self.assertRaisesRegex(RuntimeError, "runner limit reached"),
        ):
            runner_control.launch(base_event)

    def test_launch_rolls_back_a_malformed_ec2_response(self):
        clients["ec2"].run_instances.return_value = {"Instances": []}
        jit = {"encoded_jit_config": "encoded", "runner": {"id": 42}}
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
            self.assertRaises(IndexError),
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

    def test_reports_runner_online_status(self):
        response = {
            "runners": [
                {"name": "some-other-runner", "status": "online"},
                {"name": "reaveros-123-1-tests", "status": "online"},
            ],
        }
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                return_value=response,
            ) as github_request,
        ):
            self.assertEqual(
                runner_control.status({"runner_name": "reaveros-123-1-tests"}),
                {"online": True},
            )

        github_request.assert_called_once_with(
            "/orgs/reaver-project/actions/runners?name=reaveros-123-1-tests",
            "token",
        )

    def test_reports_missing_or_offline_runners_as_offline(self):
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request") as github_request,
        ):
            github_request.side_effect = [
                {"runners": []},
                {"runners": [{
                    "name": "reaveros-123-1-tests",
                    "status": "offline",
                }]},
            ]
            event = {"runner_name": "reaveros-123-1-tests"}
            self.assertEqual(runner_control.status(event), {"online": False})
            self.assertEqual(runner_control.status(event), {"online": False})

    def test_console_output_reports_aws_errors(self):
        clients["ec2"].get_console_output.side_effect = ClientError("AccessDenied")
        self.assertEqual(
            runner_control.console_output("i-123abc"),
            "Could not read EC2 console output: ClientError",
        )

    def test_cleanup_tolerates_resources_that_are_already_absent(self):
        clients["ssm"].delete_parameter.side_effect = ClientError(
            "ParameterNotFound"
        )
        not_found = runner_control.GitHubRequestError(
            "DELETE",
            "/runner/42",
            404,
            "not found",
        )
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=not_found,
            ),
        ):
            runner_control.cleanup_registration("/jit/already-gone", 42)

        runner_control.cleanup_registration(None, None)

    def test_delete_github_runner_propagates_non_not_found_errors(self):
        failure = runner_control.GitHubRequestError(
            "DELETE",
            "/runner/42",
            500,
            "server error",
        )
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=failure,
            ),
            self.assertRaises(runner_control.GitHubRequestError),
        ):
            runner_control.delete_github_runner(42)

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

    def test_terminate_rejects_an_unowned_instance(self):
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
            self.assertRaisesRegex(ValueError, "not a ReaverOS runner"),
        ):
            runner_control.terminate({"instance_id": "i-123abc"})

    def test_terminate_stops_the_instance_after_cleanup_failure(self):
        instance = {"InstanceId": "i-123abc", "Tags": []}
        failure = RuntimeError("cleanup failed")
        with (
            mock.patch.object(
                runner_control,
                "runner_instances",
                return_value=[instance],
            ),
            mock.patch.object(
                runner_control,
                "cleanup_registration",
                side_effect=failure,
            ),
            mock.patch.object(
                runner_control,
                "console_output",
                return_value="output",
            ),
            self.assertRaisesRegex(RuntimeError, "cleanup failed"),
        ):
            runner_control.terminate({"instance_id": "i-123abc"})

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

    def test_reap_reports_cleanup_failure_after_terminating_runner(self):
        old_runner = {
            "InstanceId": "i-old",
            "LaunchTime": datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
            "State": {"Name": "running"},
            "Tags": [],
        }
        terminated_runner = {
            "InstanceId": "i-terminated",
            "LaunchTime": datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC),
            "State": {"Name": "terminated"},
            "Tags": [],
        }
        with (
            mock.patch.object(
                runner_control,
                "runner_instances",
                return_value=[old_runner, terminated_runner],
            ),
            mock.patch.object(
                runner_control,
                "cleanup_registration",
                side_effect=RuntimeError("cleanup failed"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "expired runner cleanup failed: i-old",
            ),
        ):
            runner_control.reap({})

        clients["ec2"].terminate_instances.assert_called_once_with(
            InstanceIds=["i-old"],
        )

    def test_handler_dispatches_supported_actions(self):
        event = {"action": "status"}
        with mock.patch.object(
            runner_control,
            "status",
            return_value={"online": True},
        ) as status:
            self.assertEqual(
                runner_control.handler(event, None),
                {"online": True},
            )
        status.assert_called_once_with(event)

        with self.assertRaisesRegex(ValueError, "unsupported runner control action"):
            runner_control.handler({"action": "destroy-everything"}, None)

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
            mock.patch.object(
                runner_control,
                "cleanup_registration",
                side_effect=RuntimeError("cleanup failed"),
            ) as cleanup,
            mock.patch("builtins.print") as print_message,
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
        print_message.assert_called_once_with(
            "Runner launch rollback was incomplete: cleanup failed"
        )
        clients["ec2"].run_instances.assert_not_called()


if __name__ == "__main__":
    unittest.main()
