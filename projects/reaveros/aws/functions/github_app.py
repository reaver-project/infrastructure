import base64
import json
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class GitHubRequestError(RuntimeError):
    def __init__(self, method, path, status, detail):
        super().__init__(f"GitHub {method} {path} failed: {status} {detail}")
        self.status = status


def base64_url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_app_jwt(credentials, now=None):
    if now is None:
        now = int(time.time())
    header = base64_url(
        json.dumps(
            {"alg": "RS256", "typ": "JWT"},
            separators=(",", ":"),
        ).encode()
    )
    payload = base64_url(
        json.dumps(
            {
                "exp": now + 540,
                "iat": now - 60,
                "iss": credentials["app_id"],
            },
            separators=(",", ":"),
        ).encode()
    )
    unsigned_token = f"{header}.{payload}".encode()
    private_key = serialization.load_pem_private_key(
        credentials["private_key"].encode(),
        password=None,
    )
    signature = private_key.sign(
        unsigned_token,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{unsigned_token.decode()}.{base64_url(signature)}"


def retry_delay(error, attempt):
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0), 30)
        except ValueError:
            pass
    return 2**attempt


def request(path, token, method="GET", body=None, *, user_agent):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent,
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    github_request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers=headers,
    )
    retry_statuses = {429, 500, 502, 503, 504}
    attempts = 3
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(  # noqa: S310 - the URL is fixed to GitHub HTTPS.
                github_request,
                timeout=30,
            ) as response:
                if response.status == 204:
                    return None
                return json.load(response)
        except urllib.error.HTTPError as error:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            retriable = error.code in retry_statuses or (
                error.code == 403 and retry_after is not None
            )
            if retriable and attempt + 1 < attempts:
                time.sleep(retry_delay(error, attempt))
                continue
            detail = error.read().decode(errors="replace")
            raise GitHubRequestError(
                method,
                path,
                error.code,
                detail,
            ) from error
        except urllib.error.URLError as error:
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            raise GitHubRequestError(
                method,
                path,
                "network error",
                str(error.reason),
            ) from error
    raise AssertionError("unreachable")
