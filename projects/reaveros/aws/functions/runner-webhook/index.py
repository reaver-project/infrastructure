import json
import logging
import os
import re

import boto3
from lib import normalize_job_event
from webhook import event_body, event_header, parse_payload, verify_signature

secrets = boto3.client("secretsmanager")
sqs = boto3.client("sqs")
cached_webhook_secret = None
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
delivery_pattern = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def webhook_secret():
    global cached_webhook_secret

    if cached_webhook_secret is None:
        response = secrets.get_secret_value(SecretId=os.environ["RUNNER_WEBHOOK_SECRET_ID"])
        value = response.get("SecretString")
        if not isinstance(value, str) or len(value) < 32:
            raise RuntimeError("runner webhook secret is invalid")
        cached_webhook_secret = value
    return cached_webhook_secret


def response(status_code, message):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"message": message}, separators=(",", ":")),
    }


def handler(event, _context):
    try:
        body = event_body(event)
        signature = event_header(event, "x-hub-signature-256")
        if not verify_signature(body, signature, webhook_secret()):
            return response(401, "invalid webhook signature")

        event_name = event_header(event, "x-github-event")
        if event_name == "ping":
            return response(200, "pong")
        if event_name != "workflow_job":
            return response(200, "ignored webhook event")

        payload = parse_payload(body)
        allowed = {
            name.strip().casefold()
            for name in os.environ["ALLOWED_REPOSITORIES"].split(",")
            if name.strip()
        }
        normalized = normalize_job_event(payload, allowed)
        if normalized is None:
            return response(200, "ignored workflow job")

        trust, job = normalized
        delivery_id = event_header(event, "x-github-delivery")
        if delivery_pattern.fullmatch(delivery_id) is None:
            raise ValueError("GitHub delivery ID is invalid")
        queue_url = os.environ[f"{trust.upper()}_RUNNER_QUEUE_URL"]
        queued = sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(
                {"schema_version": 1, "delivery_id": delivery_id, "job": job},
                separators=(",", ":"),
            ),
            MessageGroupId=f"job-{job['job_id']}",
            MessageDeduplicationId=delivery_id,
        )
        if (
            not isinstance(queued, dict)
            or not isinstance(queued.get("MessageId"), str)
            or not queued["MessageId"]
        ):
            raise RuntimeError("SQS did not acknowledge the runner webhook")
        logger.info("queued runner delivery %s for job %d", delivery_id, job["job_id"])
        return response(202, "queued workflow job")
    except ValueError as error:
        return response(400, str(error))
