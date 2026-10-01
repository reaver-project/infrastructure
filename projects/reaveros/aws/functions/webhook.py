import base64
import hashlib
import hmac
import json


def event_body(event, maximum_size=1024 * 1024):
    body = event.get("body")
    if not isinstance(body, str):
        raise ValueError("webhook body is missing")
    if event.get("isBase64Encoded", False):
        try:
            body = base64.b64decode(body, validate=True)
        except ValueError as error:
            raise ValueError("webhook body is not valid base64") from error
    else:
        body = body.encode()
    if len(body) > maximum_size:
        raise ValueError("webhook body is too large")
    return body


def event_header(event, name):
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return ""
    name = name.casefold()
    return next(
        (
            value
            for key, value in headers.items()
            if isinstance(key, str) and key.casefold() == name and isinstance(value, str)
        ),
        "",
    )


def verify_signature(body, signature, secret):
    if not isinstance(secret, str) or not secret:
        raise ValueError("webhook secret is missing")
    expected = (
        "sha256="
        + hmac.new(
            secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(signature, expected)


def parse_payload(body):
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("webhook body is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("webhook payload must be an object")
    return payload
