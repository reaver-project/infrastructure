import hashlib
import hmac
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

clients = {"secretsmanager": mock.Mock(), "sqs": mock.Mock()}
boto3 = types.ModuleType("boto3")
boto3.client = lambda name: clients[name]
sys.modules["boto3"] = boto3

module_directory = pathlib.Path(__file__).parent


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    sys.modules[name] = module
    return module


load_module("webhook", module_directory.parent / "webhook.py")
load_module("lib", module_directory / "lib.py")
runner_webhook = load_module("runner_webhook_index", module_directory / "index.py")

repository = "reaver-project/reaveros"
sha = "0123456789abcdef0123456789abcdef01234567"
delivery_id = "01234567-89ab-cdef-0123-456789abcdef"
secret = "test-runner-webhook-secret-with-32-bytes"
environment = {
    "RUNNER_WEBHOOK_SECRET_ID": "runner-webhook-secret",
    "ALLOWED_REPOSITORIES": repository,
    "CANDIDATE_RUNNER_QUEUE_URL": "candidate-queue",
    "TRUSTED_RUNNER_QUEUE_URL": "trusted-queue",
}


def job_event(*, branch="main", action="queued", labels=None):
    if labels is None:
        labels = ["self-hosted", "reaveros-aws", "reaveros-456-2-unit-tests-amd64"]
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
            "labels": labels,
        },
    }


def signed_event(payload, *, event_name="workflow_job", key=secret, guid=delivery_id):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return {
        "body": body.decode(),
        "headers": {
            "x-github-event": event_name,
            "x-github-delivery": guid,
            "x-hub-signature-256": signature,
        },
    }


class RunnerWebhookIngressTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict("os.environ", environment)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        runner_webhook.cached_webhook_secret = None
        clients["secretsmanager"].reset_mock()
        clients["sqs"].reset_mock()
        clients["secretsmanager"].get_secret_value.return_value = {"SecretString": secret}
        clients["sqs"].send_message.return_value = {"MessageId": "message-1"}

    def test_queues_main_job_by_job_id_for_the_trusted_controller(self):
        response = runner_webhook.handler(signed_event(job_event()), None)
        self.assertEqual(response["statusCode"], 202)
        arguments = clients["sqs"].send_message.call_args.kwargs
        self.assertEqual(arguments["QueueUrl"], "trusted-queue")
        self.assertEqual(arguments["MessageGroupId"], "runner-controller")
        self.assertEqual(arguments["MessageDeduplicationId"], delivery_id)
        task = json.loads(arguments["MessageBody"])
        self.assertEqual(task["delivery_id"], delivery_id)
        self.assertEqual(task["job"]["job_id"], 123)
        self.assertEqual(task["job"]["head_sha"], sha)
        self.assertNotIn("private_key", task["job"])

    def test_routes_copied_pr_and_completion_to_candidate_queue(self):
        response = runner_webhook.handler(
            signed_event(job_event(branch="pull-request/247", action="completed")), None
        )
        self.assertEqual(response["statusCode"], 202)
        arguments = clients["sqs"].send_message.call_args.kwargs
        self.assertEqual(arguments["QueueUrl"], "candidate-queue")
        self.assertEqual(json.loads(arguments["MessageBody"])["job"]["action"], "completed")

    def test_rejects_bad_signature_before_parsing_or_queueing(self):
        response = runner_webhook.handler(signed_event(job_event(), key="different"), None)
        self.assertEqual(response["statusCode"], 401)
        clients["sqs"].send_message.assert_not_called()

    def test_ignores_ping_and_unrelated_jobs_after_authentication(self):
        ping = runner_webhook.handler(signed_event({}, event_name="ping"), None)
        self.assertEqual(ping["statusCode"], 200)
        other_event = runner_webhook.handler(signed_event({}, event_name="push"), None)
        self.assertEqual(other_event["statusCode"], 200)
        unrelated = runner_webhook.handler(signed_event(job_event(labels=["ubuntu-latest"])), None)
        self.assertEqual(unrelated["statusCode"], 200)
        clients["sqs"].send_message.assert_not_called()

    def test_rejects_malformed_delivery_and_job(self):
        invalid_delivery = runner_webhook.handler(signed_event(job_event(), guid="bad"), None)
        self.assertEqual(invalid_delivery["statusCode"], 400)
        invalid_job = job_event()
        invalid_job["workflow_job"]["head_branch"] = "feature/unreviewed"
        rejected = runner_webhook.handler(signed_event(invalid_job), None)
        self.assertEqual(rejected["statusCode"], 400)
        clients["sqs"].send_message.assert_not_called()

    def test_does_not_acknowledge_an_unstored_event(self):
        clients["sqs"].send_message.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "did not acknowledge"):
            runner_webhook.handler(signed_event(job_event()), None)

    def test_rejects_unconfigured_secret_as_a_server_error(self):
        clients["secretsmanager"].get_secret_value.return_value = {"SecretString": "short"}
        with self.assertRaisesRegex(RuntimeError, "secret is invalid"):
            runner_webhook.handler(signed_event(job_event()), None)
        clients["sqs"].send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
