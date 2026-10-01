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
    "sqs": mock.Mock(),
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

sys.modules.update(
    {
        "boto3": boto3,
        "botocore": botocore,
        "botocore.exceptions": botocore_exceptions,
        "cryptography": types.ModuleType("cryptography"),
        "cryptography.hazmat": types.ModuleType("cryptography.hazmat"),
        "cryptography.hazmat.primitives": primitives,
        "cryptography.hazmat.primitives.asymmetric": asymmetric,
    }
)

module_directory = pathlib.Path(__file__).parent
github_app_specification = importlib.util.spec_from_file_location(
    "github_app",
    module_directory.parent / "github_app.py",
)
github_app = importlib.util.module_from_spec(github_app_specification)
github_app_specification.loader.exec_module(github_app)
sys.modules["github_app"] = github_app
lib_specification = importlib.util.spec_from_file_location(
    "lib",
    module_directory / "lib.py",
)
lib = importlib.util.module_from_spec(lib_specification)
lib_specification.loader.exec_module(lib)
sys.modules["lib"] = lib
workflow_job_specification = importlib.util.spec_from_file_location(
    "workflow_job",
    module_directory / "workflow_job.py",
)
workflow_job_module = importlib.util.module_from_spec(workflow_job_specification)
workflow_job_specification.loader.exec_module(workflow_job_module)
sys.modules["workflow_job"] = workflow_job_module
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
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ALLOWED_REPOSITORIES": "reaver-project/reaveros",
                "AWS_REGION": "us-west-2",
                "BUILDER_PROFILE_ARN": "arn:builder",
                "CACHE_TRUST_CLASS": "candidate",
                "GITHUB_ORGANIZATION": "reaver-project",
                "GITHUB_RUNNER_GROUP_ID": "7",
                "GITHUB_APP_SECRET_ID": "runner-app-secret",
                "JIT_PARAMETER_PREFIX": "/jit/",
                "LARGE_INSTANCE_TYPE": "c8i.8xlarge",
                "LAUNCH_TEMPLATE_ID": "lt-123",
                "MAXIMUM_CONCURRENT_RUNNERS": "4",
                "MAXIMUM_PARALLEL_CONTROLLER_LAUNCHES": "2",
                "MAXIMUM_AGE_MINUTES": "180",
                "MEDIUM_INSTANCE_TYPE": "c8i.4xlarge",
                "RUNNER_INSTANCE_NAME": "reaveros-runner",
                "RUNNER_QUEUE_URL": "runner-queue",
                "RUNNER_WEBHOOK_SECRET_ID": "runner-webhook-secret",
                "RUNNER_WEBHOOK_URL": "https://example.lambda-url.us-west-2.on.aws/",
                "VALIDATION_PROFILE_ARN": "arn:validation",
            },
        )
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
            return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

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

    def test_trusted_controller_configures_the_app_webhook_without_exposing_secrets(self):
        url = "https://example.lambda-url.us-west-2.on.aws/"
        clients["secretsmanager"].get_secret_value.side_effect = [
            {"SecretString": json.dumps({"app_id": 1234, "private_key": "key"})},
            {"SecretString": "a" * 64},
        ]
        with (
            mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}),
            mock.patch.object(runner_control, "create_app_jwt", return_value="app-jwt"),
            mock.patch.object(
                runner_control, "github_request", return_value={"url": url}
            ) as github_request,
        ):
            self.assertEqual(runner_control.configure_webhook({}), {"configured": True})
        self.assertEqual(
            github_request.call_args.args,
            (
                "/app/hook/config",
                "app-jwt",
                "PATCH",
                {"url": url, "content_type": "json", "secret": "a" * 64},
            ),
        )
        with self.assertRaisesRegex(ValueError, "only the trusted controller"):
            runner_control.configure_webhook({})

        clients["secretsmanager"].get_secret_value.side_effect = [
            {"SecretString": json.dumps({"app_id": 1234, "private_key": "key"})},
            {"SecretString": "too short"},
        ]
        with (
            mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}),
            self.assertRaisesRegex(ValueError, "secret is invalid"),
        ):
            runner_control.configure_webhook({})

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

    def test_public_github_requests_do_not_send_an_installation_token(self):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b"[]"
        with mock.patch.object(
            runner_control.urllib.request,
            "urlopen",
            return_value=mock.MagicMock(__enter__=mock.Mock(return_value=response)),
        ) as urlopen:
            self.assertEqual(runner_control.github_request("/public", None), [])
        self.assertIsNone(urlopen.call_args.args[0].get_header("Authorization"))

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
            "SecretString": json.dumps(
                {
                    "app_id": 1234,
                    "installation_id": 5678,
                    "private_key": "test-private-key",
                }
            ),
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

    def test_admits_only_copied_workflows_to_the_restricted_runner_group(self):
        main = "reaver-project/reaveros/.github/workflows/aws-runner.yml@refs/heads/main"
        copied = (
            "reaver-project/reaveros/.github/workflows/aws-runner.yml@refs/heads/pull-request/17"
        )
        stale = (
            "reaver-project/reaveros/.github/workflows/aws-runner.yml@refs/heads/pull-request/16"
        )
        group = {
            "name": "reaveros",
            "visibility": "selected",
            "allows_public_repositories": True,
            "restricted_to_workflows": True,
            "selected_workflows": [main],
        }
        with mock.patch.object(
            runner_control,
            "github_request",
            side_effect=[
                group,
                [{"ref": "refs/heads/pull-request/17"}],
                {**group, "selected_workflows": [main, copied]},
            ],
        ) as request:
            runner_control.ensure_workflow_access(
                "token", "reaver-project/reaveros", "refs/heads/pull-request/17"
            )
        path = "/orgs/reaver-project/actions/runner-groups/7"
        self.assertEqual(request.call_args_list[0], mock.call(path, "token"))
        self.assertEqual(
            request.call_args_list[1],
            mock.call(
                "/repos/reaver-project/reaveros/git/matching-refs/heads/pull-request/",
                None,
            ),
        )
        self.assertEqual(
            request.call_args_list[2],
            mock.call(
                path,
                "token",
                "PATCH",
                {"selected_workflows": [main, copied]},
            ),
        )

        with mock.patch.object(
            runner_control,
            "github_request",
            side_effect=[
                {**group, "selected_workflows": [main, stale]},
                [{"ref": "refs/heads/pull-request/17"}],
                {**group, "selected_workflows": [main, copied]},
            ],
        ) as request:
            runner_control.ensure_workflow_access(
                "token", "reaver-project/reaveros", "refs/heads/pull-request/17"
            )
        self.assertEqual(
            request.call_args_list[2].args[3],
            {"selected_workflows": [main, copied]},
        )

        with mock.patch.object(
            runner_control,
            "github_request",
            side_effect=[
                {**group, "selected_workflows": [main, stale]},
                [{"ref": "refs/heads/pull-request/17"}],
                {**group, "selected_workflows": [main, copied, stale]},
            ],
        ) as request:
            runner_control.ensure_workflow_access(
                "token",
                "reaver-project/reaveros",
                "refs/heads/pull-request/17",
                ["refs/heads/pull-request/16"],
            )
        self.assertEqual(
            request.call_args_list[2].args[3],
            {"selected_workflows": [main, stale, copied]},
        )

        with mock.patch.object(runner_control, "github_request", return_value=group) as request:
            runner_control.ensure_workflow_access(
                "token", "reaver-project/reaveros", "refs/heads/main"
            )
        request.assert_called_once_with(path, "token")

        with (
            mock.patch.object(
                runner_control,
                "github_request",
                return_value={**group, "restricted_to_workflows": False},
            ),
            self.assertRaisesRegex(ValueError, "not restricted"),
        ):
            runner_control.ensure_workflow_access(
                "token", "reaver-project/reaveros", "refs/heads/pull-request/17"
            )
        with (
            mock.patch.object(
                runner_control,
                "github_request",
                return_value={**group, "selected_workflows": ["other/repo/workflow.yml@main"]},
            ),
            self.assertRaisesRegex(ValueError, "unexpected workflow"),
        ):
            runner_control.ensure_workflow_access(
                "token", "reaver-project/reaveros", "refs/heads/pull-request/17"
            )
        with (
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=[group, [{"ref": "refs/heads/pull-request/17"}], group],
            ),
            self.assertRaisesRegex(RuntimeError, "restricted workflow access"),
        ):
            runner_control.ensure_workflow_access(
                "token", "reaver-project/reaveros", "refs/heads/pull-request/17"
            )

    def test_live_workflow_refs_keep_only_valid_runners_for_the_repository(self):
        instances = [
            {
                "Tags": [
                    {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                    {"Key": "GitHubSourceRef", "Value": "refs/heads/pull-request/16"},
                ]
            },
            {
                "Tags": [
                    {"Key": "GitHubRepository", "Value": "another/repository"},
                    {"Key": "GitHubSourceRef", "Value": "refs/heads/pull-request/17"},
                ]
            },
            {
                "Tags": [
                    {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                    {"Key": "GitHubSourceRef", "Value": "refs/heads/not-a-copied-ref"},
                ]
            },
        ]
        self.assertEqual(
            runner_control.live_workflow_refs(instances, "reaver-project/reaveros"),
            ["refs/heads/pull-request/16"],
        )

    def test_reaper_prunes_only_refs_without_a_copied_branch_or_live_runner(self):
        prefix = "reaver-project/reaveros/.github/workflows/aws-runner.yml@"
        main = prefix + "refs/heads/main"
        copied = prefix + "refs/heads/pull-request/17"
        stale = prefix + "refs/heads/pull-request/18"
        group = {
            "name": "reaveros",
            "visibility": "selected",
            "allows_public_repositories": True,
            "restricted_to_workflows": True,
            "selected_workflows": [main, copied, stale],
        }
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=[
                    [
                        {"ref": "refs/heads/pull-request/17"},
                        {"ref": "refs/heads/pull-request/wip"},
                    ],
                    group,
                    {**group, "selected_workflows": [main, copied]},
                ],
            ) as request,
        ):
            runner_control.prune_workflow_access("reaver-project/reaveros")
        self.assertEqual(request.call_args_list[0].args[1], None)
        self.assertEqual(
            request.call_args_list[2].args[3],
            {"selected_workflows": [main, copied]},
        )

        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=[
                    [],
                    group,
                    {**group, "selected_workflows": [main, stale]},
                ],
            ),
        ):
            runner_control.prune_workflow_access(
                "reaver-project/reaveros", ["refs/heads/pull-request/18"]
            )
        with (
            mock.patch.object(runner_control, "github_request", return_value={}),
            self.assertRaisesRegex(ValueError, "copied CI refs"),
        ):
            runner_control.prune_workflow_access("reaver-project/reaveros")
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request", side_effect=[[], group, group]),
            self.assertRaisesRegex(RuntimeError, "restricted workflow access"),
        ):
            runner_control.prune_workflow_access("reaver-project/reaveros")

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
        filters = [
            {
                "Name": "tag:ReaverOSPurpose",
                "Values": ["GitHubActionsRunner"],
            }
        ]
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
            [
                {
                    "Type": "Expiration",
                    "Version": "1.0",
                    "Attributes": {"Timestamp": "2030-01-01T04:00:00Z"},
                }
            ],
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
            mock.patch.object(runner_control, "ensure_workflow_access") as access,
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
            result = runner_control.launch(
                {
                    "github_run_attempt": 2,
                    "github_run_id": 123,
                    "repository": "reaver-project/reaveros",
                    "source_ref": "refs/heads/pull-request/17",
                    "runner_key": "unit-tests-amd64",
                    "runner_profile": "validation",
                    "runner_size": "medium",
                }
            )

        self.assertEqual(
            result,
            {
                "instance_id": "i-launched",
                "labels": [
                    "self-hosted",
                    "reaveros-aws",
                    "reaveros-123-2-unit-tests-amd64",
                ],
                "runner_name": "reaveros-123-2-unit-tests-amd64",
            },
        )
        github_request.assert_called_once()
        access.assert_called_once_with(
            "token", "reaver-project/reaveros", "refs/heads/pull-request/17", []
        )
        put_parameter = clients["ssm"].put_parameter.call_args.kwargs
        self.assertEqual(put_parameter["Tier"], "Advanced")
        self.assertEqual(
            json.loads(put_parameter["Policies"])[0]["Type"],
            "Expiration",
        )
        clients["ec2"].run_instances.assert_called_once()
        tags = clients["ec2"].run_instances.call_args.kwargs["TagSpecifications"][0]["Tags"]
        self.assertIn(
            {"Key": "ReaverProjectCacheTrust", "Value": "candidate"},
            tags,
        )

    def test_webhook_runner_launch_is_idempotent_for_the_job(self):
        event = {
            "github_run_attempt": 2,
            "github_run_id": 123,
            "github_job_id": 456,
            "repository": "reaver-project/reaveros",
            "source_ref": "refs/heads/pull-request/17",
            "runner_key": "unit-tests-amd64",
            "runner_profile": "validation",
            "runner_size": "medium",
        }
        instance = {
            "InstanceId": "i-existing",
            "State": {"Name": "running"},
            "Tags": [
                {"Key": "GitHubJobId", "Value": "456"},
                {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                {"Key": "GitHubSourceRef", "Value": "refs/heads/pull-request/17"},
                {"Key": "GitHubRunId", "Value": "123"},
                {"Key": "GitHubRunnerName", "Value": "reaveros-123-2-unit-tests-amd64"},
                {"Key": "ReaverOSRunnerProfile", "Value": "validation"},
                {"Key": "ReaverOSRunnerSize", "Value": "medium"},
                {"Key": "ReaverProjectCacheTrust", "Value": "candidate"},
            ],
        }
        with mock.patch.object(runner_control, "runner_instances", return_value=[instance]):
            result = runner_control.launch(event)
        self.assertEqual(result["instance_id"], "i-existing")
        self.assertEqual(result["runner_name"], "reaveros-123-2-unit-tests-amd64")
        clients["ec2"].run_instances.assert_not_called()
        clients["ssm"].put_parameter.assert_not_called()

        invalid = {
            **instance,
            "Tags": [*instance["Tags"], {"Key": "ReaverProjectCacheTrust", "Value": "trusted"}],
        }
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[invalid]),
            self.assertRaisesRegex(ValueError, "differs from the request"),
        ):
            runner_control.launch(event)
        with (
            mock.patch.object(
                runner_control, "runner_instances", return_value=[instance, instance]
            ),
            self.assertRaisesRegex(RuntimeError, "multiple runners"),
        ):
            runner_control.launch(event)
        with (
            mock.patch.object(
                runner_control,
                "runner_instances",
                return_value=[{**instance, "State": {"Name": "stopping"}}],
            ),
            self.assertRaisesRegex(RuntimeError, "shutting down"),
        ):
            runner_control.launch(event)

    def test_queued_webhook_revalidates_github_before_launch(self):
        job = {
            "action": "queued",
            "repository": "reaver-project/reaveros",
            "repository_id": 5,
            "installation_id": 17,
            "job_id": 456,
            "run_id": 123,
            "run_attempt": 2,
            "head_branch": "pull-request/17",
            "head_sha": "a" * 40,
            "job_name": "Unit tests (amd64) / Run AWS unit-tests amd64",
            "runner_key": "unit-tests-amd64",
            "runner_name": "reaveros-123-2-unit-tests-amd64",
        }
        fetched_job = {
            "id": 456,
            "run_id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "head_branch": "pull-request/17",
            "name": job["job_name"],
            "workflow_name": "CI",
            "labels": ["self-hosted", "reaveros-aws", job["runner_name"]],
            "runner_group_id": None,
            "status": "queued",
        }
        run = {
            "id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "head_branch": "pull-request/17",
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "repository": {"full_name": "reaver-project/reaveros", "id": 5},
            "event": "push",
            "status": "in_progress",
        }
        delivery = {"schema_version": 1, "job": job}
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control, "github_request", side_effect=[fetched_job, run]
            ) as github_request,
            mock.patch.object(
                runner_control, "launch", return_value={"instance_id": "i-runner"}
            ) as launch,
        ):
            result = runner_control.handler(
                {"Records": [{"eventSource": "aws:sqs", "body": json.dumps(delivery)}]}, None
            )
        self.assertEqual(result, {"instance_id": "i-runner"})
        self.assertEqual(launch.call_args.args[0]["runner_profile"], "validation")
        self.assertEqual(launch.call_args.args[0]["runner_size"], "medium")
        self.assertEqual(launch.call_args.args[0]["github_job_id"], 456)
        self.assertEqual(github_request.call_count, 2)

        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request", side_effect=[fetched_job, run]),
            mock.patch.object(runner_control, "launch") as launch,
        ):
            with self.assertRaisesRegex(ValueError, "head_sha differs"):
                runner_control.workflow_job(
                    {"schema_version": 1, "job": {**job, "head_sha": "b" * 40}}
                )
            launch.assert_not_called()

        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=[{**fetched_job, "status": "completed"}, run],
            ),
            mock.patch.object(runner_control, "launch") as launch,
        ):
            self.assertEqual(
                runner_control.workflow_job(delivery),
                {"ignored": "workflow job is no longer queued"},
            )
            launch.assert_not_called()

        with self.assertRaisesRegex(ValueError, "one record"):
            runner_control.handler({"Records": []}, None)
        with self.assertRaisesRegex(ValueError, "not from SQS"):
            runner_control.handler({"Records": [{"eventSource": "other"}]}, None)

        for invalid, message in (
            ({"schema_version": 2, "job": job}, "schema"),
            ({"schema_version": 1}, "job is missing"),
            ({"schema_version": 1, "job": {**job, "job_id": True}}, "job_id"),
            ({"schema_version": 1, "job": {**job, "action": "other"}}, "action"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                runner_control.workflow_job(invalid)

    def test_sqs_worker_retries_capacity_without_reporting_an_error(self):
        event = {
            "Records": [
                {
                    "eventSource": "aws:sqs",
                    "body": "{}",
                    "messageId": "message-1",
                    "receiptHandle": "receipt-1",
                }
            ]
        }
        with mock.patch.object(
            runner_control,
            "handler",
            side_effect=RuntimeError("ephemeral ReaverOS runner limit reached"),
        ):
            self.assertEqual(
                runner_control.sqs_handler(event, None),
                {"batchItemFailures": [{"itemIdentifier": "message-1"}]},
            )
        clients["sqs"].change_message_visibility.assert_called_once_with(
            QueueUrl="runner-queue", ReceiptHandle="receipt-1", VisibilityTimeout=30
        )
        with mock.patch.object(runner_control, "handler", return_value={}):
            self.assertEqual(runner_control.sqs_handler(event, None), {"batchItemFailures": []})
        with (
            mock.patch.object(runner_control, "handler", side_effect=RuntimeError("unrelated")),
            self.assertRaisesRegex(RuntimeError, "unrelated"),
        ):
            runner_control.sqs_handler(event, None)
        with self.assertRaisesRegex(ValueError, "one record"):
            runner_control.sqs_handler({"Records": []}, None)

    def test_completed_webhook_terminates_only_matching_job_instance(self):
        job = {
            "action": "completed",
            "repository": "reaver-project/reaveros",
            "repository_id": 5,
            "installation_id": 17,
            "job_id": 456,
            "run_id": 123,
            "run_attempt": 2,
            "head_branch": "main",
            "head_sha": "a" * 40,
            "job_name": "Prepare build environment (medium) / Run AWS prepare",
            "runner_key": "prepare",
            "runner_name": "reaveros-123-2-prepare",
        }
        fetched_job = {
            "id": 456,
            "run_id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "name": job["job_name"],
            "workflow_name": "CI",
            "labels": ["self-hosted", "reaveros-aws", job["runner_name"]],
            "runner_group_id": 7,
            "status": "completed",
        }
        run = {
            "id": 123,
            "run_attempt": 2,
            "head_sha": "a" * 40,
            "head_branch": "main",
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "repository": {"full_name": "reaver-project/reaveros", "id": 5},
            "event": "push",
        }
        instance = {
            "InstanceId": "i-abc123",
            "State": {"Name": "running"},
            "Tags": [
                {"Key": "GitHubJobId", "Value": "456"},
                {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                {"Key": "GitHubRunId", "Value": "123"},
                {"Key": "GitHubSourceRef", "Value": "refs/heads/main"},
                {"Key": "GitHubRunnerName", "Value": "reaveros-123-2-prepare"},
                {"Key": "ReaverProjectCacheTrust", "Value": "trusted"},
            ],
        }
        with (
            mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request", side_effect=[fetched_job, run]),
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(
                runner_control, "terminate", return_value={"instance_id": "i-abc123"}
            ) as terminate,
        ):
            self.assertEqual(
                runner_control.workflow_job({"schema_version": 1, "job": job}),
                {"instance_id": "i-abc123"},
            )
            terminate.assert_called_once_with({"instance_id": "i-abc123"})

        with (
            mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request", side_effect=[fetched_job, run]),
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
        ):
            self.assertEqual(
                runner_control.workflow_job({"schema_version": 1, "job": job}),
                {"ignored": "workflow job has no active runner"},
            )

        for instances, message in (
            ([{**instance, "State": {"Name": "terminated"}}], "no active runner"),
            ([{**instance, "Tags": []}], "no active runner"),
            (
                [{**instance, "Tags": [*instance["Tags"], {"Key": "GitHubRunId", "Value": "42"}]}],
                "tags differ",
            ),
            ([instance, instance], "multiple runners"),
        ):
            with (
                self.subTest(message=message),
                mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}),
                mock.patch.object(runner_control, "github_token", return_value="token"),
                mock.patch.object(runner_control, "github_request", side_effect=[fetched_job, run]),
                mock.patch.object(runner_control, "runner_instances", return_value=instances),
            ):
                if message == "no active runner":
                    self.assertEqual(
                        runner_control.workflow_job({"schema_version": 1, "job": job}),
                        {"ignored": "workflow job has no active runner"},
                    )
                else:
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        runner_control.workflow_job({"schema_version": 1, "job": job})

        with (
            mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control,
                "github_request",
                side_effect=[{**fetched_job, "status": "in_progress"}, run],
            ),
            self.assertRaisesRegex(RuntimeError, "not yet visible"),
        ):
            runner_control.workflow_job({"schema_version": 1, "job": job})

    def test_trusted_controller_uses_only_the_trusted_builder_profile(self):
        clients["ec2"].run_instances.return_value = {
            "Instances": [{"InstanceId": "i-trusted"}],
        }
        with (
            mock.patch.dict(
                os.environ,
                {"BUILDER_PROFILE_ARN": "arn:trusted-builder", "CACHE_TRUST_CLASS": "trusted"},
            ),
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "ensure_workflow_access"),
            mock.patch.object(
                runner_control,
                "github_request",
                return_value={"encoded_jit_config": "encoded", "runner": {"id": 43}},
            ),
        ):
            runner_control.launch(
                {
                    "github_run_attempt": 1,
                    "github_run_id": 124,
                    "repository": "reaver-project/reaveros",
                    "source_ref": "refs/heads/main",
                    "runner_key": "prepare",
                    "runner_profile": "builder",
                    "runner_size": "large",
                }
            )

        launch = clients["ec2"].run_instances.call_args.kwargs
        self.assertEqual(launch["IamInstanceProfile"], {"Arn": "arn:trusted-builder"})
        self.assertIn(
            {"Key": "ReaverProjectCacheTrust", "Value": "trusted"},
            launch["TagSpecifications"][0]["Tags"],
        )

    def test_launch_rejects_invalid_profiles_sizes_and_capacity(self):
        base_event = {
            "github_run_attempt": 2,
            "github_run_id": 123,
            "repository": "reaver-project/reaveros",
            "source_ref": "refs/heads/pull-request/17",
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

        with (
            mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "other"}),
            self.assertRaisesRegex(ValueError, "invalid cache trust class"),
        ):
            runner_control.launch(base_event)

        live_runner = {"State": {"Name": "running"}}
        with (
            mock.patch.object(
                runner_control,
                "runner_instances",
                return_value=[live_runner] * 3,
            ),
            self.assertRaisesRegex(RuntimeError, "runner limit reached"),
        ):
            runner_control.launch(base_event)

        with (
            mock.patch.dict(os.environ, {"MAXIMUM_CONCURRENT_RUNNERS": "1"}),
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "runner limit reached"),
        ):
            runner_control.launch(base_event)

    def test_launch_rolls_back_a_malformed_ec2_response(self):
        clients["ec2"].run_instances.return_value = {"Instances": []}
        jit = {"encoded_jit_config": "encoded", "runner": {"id": 42}}
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "ensure_workflow_access"),
            mock.patch.object(runner_control, "github_request", return_value=jit),
            mock.patch.object(runner_control, "cleanup_registration") as cleanup,
            mock.patch.object(
                lib.secrets,
                "token_hex",
                return_value="0123456789abcdef0123456789abcdef",
            ),
            self.assertRaises(IndexError),
        ):
            runner_control.launch(
                {
                    "github_run_attempt": 2,
                    "github_run_id": 123,
                    "repository": "reaver-project/reaveros",
                    "source_ref": "refs/heads/pull-request/17",
                    "runner_key": "unit-tests-amd64",
                    "runner_profile": "validation",
                    "runner_size": "medium",
                }
            )

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
                {
                    "runners": [
                        {
                            "name": "reaveros-123-1-tests",
                            "status": "offline",
                        }
                    ]
                },
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

    def test_console_output_accepts_predecoded_text(self):
        clients["ec2"].get_console_output.return_value = {
            "Output": "[    0.000000] démarrage du runner",
        }
        self.assertEqual(
            runner_control.console_output("i-123abc"),
            "[    0.000000] démarrage du runner",
        )

    def test_cleanup_tolerates_resources_that_are_already_absent(self):
        clients["ssm"].delete_parameter.side_effect = ClientError("ParameterNotFound")
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
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-123abc",
                            "Tags": [
                                {"Key": "GitHubRunnerId", "Value": "42"},
                                {"Key": "ReaverOSRunnerParameter", "Value": "/jit/real"},
                            ],
                        }
                    ]
                }
            ],
        }
        clients["ec2"].get_console_output.return_value = {
            "Output": base64.b64encode(b"runner output").decode(),
        }
        with (
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(runner_control, "github_request") as github_request,
        ):
            result = runner_control.terminate(
                {
                    "instance_id": "i-123abc",
                    "jit_parameter": "/jit/untrusted",
                    "runner_id": 99,
                }
            )

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

    def test_candidate_controller_cannot_terminate_a_trusted_runner(self):
        with (
            mock.patch.object(
                runner_control,
                "runner_instances",
                return_value=[
                    {
                        "InstanceId": "i-123abc",
                        "Tags": [{"Key": "ReaverProjectCacheTrust", "Value": "trusted"}],
                    }
                ],
            ),
            self.assertRaisesRegex(ValueError, "another cache trust class"),
        ):
            runner_control.terminate({"instance_id": "i-123abc"})

        clients["ec2"].terminate_instances.assert_not_called()

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

    def test_terminate_stops_the_instance_after_console_failure(self):
        instance = {"InstanceId": "i-123abc", "Tags": []}
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(runner_control, "cleanup_registration"),
            mock.patch.object(
                runner_control,
                "console_output",
                side_effect=ValueError("invalid console text"),
            ),
            self.assertRaisesRegex(ValueError, "invalid console text"),
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
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(runner_control, "cleanup_registration") as cleanup,
            mock.patch.object(runner_control, "prune_workflow_access") as prune,
        ):
            result = runner_control.reap({})

        cleanup.assert_called_once_with("/jit/real", 42)
        prune.assert_called_once_with("reaver-project/reaveros", [])
        clients["ec2"].terminate_instances.assert_called_once_with(
            InstanceIds=["i-123abc"],
        )
        self.assertEqual(result, {"terminated": ["i-123abc"]})

    def test_reapers_only_terminate_their_own_trust_class(self):
        expired = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
        instances = [
            {
                "InstanceId": f"i-{trust}",
                "LaunchTime": expired,
                "State": {"Name": "running"},
                "Tags": [
                    {"Key": "ReaverProjectCacheTrust", "Value": trust},
                    {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                    {"Key": "GitHubSourceRef", "Value": f"refs/heads/{trust}"},
                ],
            }
            for trust in ("candidate", "trusted")
        ]
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=instances),
            mock.patch.object(runner_control, "cleanup_registration"),
            mock.patch.object(runner_control, "prune_workflow_access") as prune,
        ):
            self.assertEqual(runner_control.reap({}), {"terminated": ["i-candidate"]})
            prune.assert_called_once_with("reaver-project/reaveros", ["refs/heads/trusted"])
            clients["ec2"].terminate_instances.assert_called_once_with(InstanceIds=["i-candidate"])

            clients["ec2"].terminate_instances.reset_mock()
            prune.reset_mock()
            with mock.patch.dict(os.environ, {"CACHE_TRUST_CLASS": "trusted"}):
                self.assertEqual(runner_control.reap({}), {"terminated": ["i-trusted"]})
            prune.assert_not_called()
            clients["ec2"].terminate_instances.assert_called_once_with(InstanceIds=["i-trusted"])

    def test_reap_preserves_a_live_runner_workflow_and_survives_prune_failure(self):
        instance = {
            "InstanceId": "i-live",
            "LaunchTime": datetime.datetime.now(datetime.UTC),
            "State": {"Name": "running"},
            "Tags": [
                {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                {"Key": "GitHubSourceRef", "Value": "refs/heads/pull-request/17"},
            ],
        }
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(
                runner_control,
                "prune_workflow_access",
                side_effect=ValueError("temporary lookup failure"),
            ) as prune,
            mock.patch("builtins.print") as print_message,
        ):
            self.assertEqual(runner_control.reap({}), {"terminated": []})
        prune.assert_called_once_with("reaver-project/reaveros", ["refs/heads/pull-request/17"])
        self.assertIn("temporary lookup failure", print_message.call_args.args[0])

    def test_reap_reconciles_completed_webhook_jobs_independently_of_the_queue(self):
        instance = {
            "InstanceId": "i-completed",
            "LaunchTime": datetime.datetime.now(datetime.UTC),
            "State": {"Name": "running"},
            "Tags": [
                {"Key": "GitHubJobId", "Value": "456"},
                {"Key": "GitHubRepository", "Value": "reaver-project/reaveros"},
                {"Key": "GitHubSourceRef", "Value": "refs/heads/pull-request/17"},
            ],
        }
        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control, "github_request", return_value={"status": "completed"}
            ),
            mock.patch.object(runner_control, "terminate") as terminate,
            mock.patch.object(runner_control, "prune_workflow_access") as prune,
        ):
            self.assertEqual(runner_control.reap({}), {"terminated": ["i-completed"]})
        terminate.assert_called_once_with({"instance_id": "i-completed"})
        prune.assert_called_once_with("reaver-project/reaveros", [])

        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control, "github_request", return_value={"status": "in_progress"}
            ),
            mock.patch.object(runner_control, "terminate") as terminate,
            mock.patch.object(runner_control, "prune_workflow_access"),
        ):
            self.assertEqual(runner_control.reap({}), {"terminated": []})
            terminate.assert_not_called()

        with (
            mock.patch.object(runner_control, "runner_instances", return_value=[instance]),
            mock.patch.object(runner_control, "github_token", return_value="token"),
            mock.patch.object(
                runner_control, "github_request", side_effect=ValueError("transient metadata error")
            ),
            mock.patch.object(runner_control, "prune_workflow_access"),
            mock.patch("builtins.print") as print_message,
        ):
            self.assertEqual(runner_control.reap({}), {"terminated": []})
        self.assertIn("transient metadata error", print_message.call_args.args[0])

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
            mock.patch.object(runner_control, "prune_workflow_access"),
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
            mock.patch.object(runner_control, "ensure_workflow_access"),
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
            runner_control.launch(
                {
                    "github_run_attempt": 2,
                    "github_run_id": 123,
                    "repository": "reaver-project/reaveros",
                    "source_ref": "refs/heads/pull-request/17",
                    "runner_key": "unit-tests-amd64",
                    "runner_profile": "validation",
                    "runner_size": "medium",
                }
            )

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
