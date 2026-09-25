import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import sys
import types
import unittest
from unittest import mock

clients = {"secretsmanager": mock.Mock()}
boto3 = types.ModuleType("boto3")
boto3.client = lambda name: clients[name]
primitives = types.ModuleType("cryptography.hazmat.primitives")
primitives.hashes = mock.Mock()
primitives.serialization = mock.Mock()
asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
asymmetric.padding = mock.Mock()
sys.modules.update(
    {
        "boto3": boto3,
        "cryptography": types.ModuleType("cryptography"),
        "cryptography.hazmat": types.ModuleType("cryptography.hazmat"),
        "cryptography.hazmat.primitives": primitives,
        "cryptography.hazmat.primitives.asymmetric": asymmetric,
    }
)

module_directory = pathlib.Path(__file__).parent


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    sys.modules[name] = module
    return module


github_app = load_module("github_app", module_directory.parent / "github_app.py")
lib = load_module("lib", module_directory / "lib.py")
ci_gate = load_module("ci_gate_index", module_directory / "index.py")

repository = "reaver-project/reaveros"
full_sha = "0123456789abcdef0123456789abcdef01234567"
new_sha = "fedcba9876543210fedcba9876543210fedcba98"


def pull_request(*, sha=full_sha, actor="external", head_repository=repository):
    return {
        "number": 12,
        "state": "open",
        "draft": False,
        "commits": 1,
        "base": {
            "ref": "main",
            "repo": {"default_branch": "main", "full_name": repository},
        },
        "head": {"sha": sha, "repo": {"full_name": head_repository}},
        "user": {"login": actor},
    }


def signed_commit(sha=full_sha, actor="griwes"):
    return {
        "commit": {
            "oid": sha,
            "parents": {"nodes": [{"oid": "0" * 40}]},
            "signature": {"isValid": True, "signer": {"login": actor}},
            "author": {"user": {"login": actor}},
        }
    }


def event_payload(*, action="opened", pr=None):
    return {
        "action": action,
        "installation": {"id": 17},
        "pull_request": pull_request() if pr is None else pr,
        "repository": {"full_name": repository, "id": 42},
    }


class CiGateIndexTests(unittest.TestCase):
    def setUp(self):
        clients["secretsmanager"].reset_mock(return_value=True, side_effect=True)
        ci_gate.cached_credentials = None
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ALLOWED_REPOSITORIES": repository,
                "AUTOMATIC_ACTORS": "griwes,reaver-project-maintenance[bot]",
                "GITHUB_APP_SECRET_ID": "ci-gate-secret",
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_loads_and_caches_complete_credentials(self):
        clients["secretsmanager"].get_secret_value.return_value = {
            "SecretString": json.dumps(
                {
                    "app_id": "1",
                    "app_slug": "reaver-project-ci-gate",
                    "private_key": "key",
                    "webhook_secret": "secret",
                }
            )
        }
        self.assertEqual(ci_gate.credentials()["app_id"], "1")
        self.assertEqual(ci_gate.credentials()["app_id"], "1")
        clients["secretsmanager"].get_secret_value.assert_called_once_with(
            SecretId="ci-gate-secret"
        )

    def test_rejects_incomplete_credentials(self):
        clients["secretsmanager"].get_secret_value.return_value = {"SecretString": '{"app_id":"1"}'}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ci_gate.credentials()

    def test_uses_the_shared_github_request_client(self):
        with mock.patch.object(ci_gate, "request", return_value={"ok": True}) as request:
            self.assertEqual(
                ci_gate.github_request("/path", "token", "POST", {"value": 1}),
                {"ok": True},
            )
        request.assert_called_once_with(
            "/path",
            "token",
            "POST",
            {"value": 1},
            user_agent="reaver-project-ci-gate",
        )

    def test_requests_a_token_scoped_to_the_event_repository(self):
        with (
            mock.patch.object(ci_gate, "create_app_jwt", return_value="jwt"),
            mock.patch.object(
                ci_gate,
                "github_request",
                return_value={"token": "installation-token"},
            ) as github_request,
        ):
            token = ci_gate.installation_token({"app_id": "1"}, 17, 42)

        self.assertEqual(token, "installation-token")
        github_request.assert_called_once_with(
            "/app/installations/17/access_tokens",
            "jwt",
            "POST",
            {
                "repository_ids": [42],
                "permissions": {
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "read",
                },
            },
        )

        with (
            mock.patch.object(ci_gate, "create_app_jwt", return_value="jwt"),
            mock.patch.object(ci_gate, "github_request", return_value={}),
            self.assertRaisesRegex(ValueError, "did not issue"),
        ):
            ci_gate.installation_token({"app_id": "1"}, 17, 42)

    def test_reads_pull_requests_and_resolves_abbreviated_revisions(self):
        with mock.patch.object(
            ci_gate,
            "github_request",
            side_effect=[pull_request(), {"sha": full_sha}],
        ) as github_request:
            self.assertEqual(ci_gate.pull_request("token", repository, 12)["number"], 12)
            self.assertEqual(
                ci_gate.resolve_revision("token", repository, full_sha[:7]),
                full_sha,
            )
        self.assertEqual(github_request.call_count, 2)

        not_found = github_app.GitHubRequestError("GET", "/commit", 404, "missing")
        with mock.patch.object(ci_gate, "github_request", side_effect=not_found):
            self.assertIsNone(ci_gate.resolve_revision("token", repository, full_sha[:7]))
        with (
            mock.patch.object(ci_gate, "github_request", return_value={"sha": "invalid"}),
            self.assertRaisesRegex(ValueError, "invalid commit SHA"),
        ):
            ci_gate.resolve_revision("token", repository, full_sha[:7])

    def test_loads_the_complete_pull_request_commit_sequence(self):
        first_page = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "totalCount": 101,
                            "nodes": [signed_commit() for _ in range(100)],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor"},
                        }
                    }
                }
            }
        }
        second_page = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "totalCount": 101,
                            "nodes": [signed_commit(new_sha)],
                            "pageInfo": {"hasNextPage": False, "endCursor": "last"},
                        }
                    }
                }
            }
        }
        with mock.patch.object(
            ci_gate, "github_request", side_effect=[first_page, second_page]
        ) as request:
            commits = ci_gate.pull_request_commits("token", repository, 12, 101)
        self.assertEqual(len(commits), 101)
        self.assertEqual(request.call_args_list[0].args[:3], ("/graphql", "token", "POST"))
        self.assertEqual(request.call_args_list[1].args[3]["variables"]["cursor"], "cursor")

        with mock.patch.object(ci_gate, "github_request", return_value=first_page):
            self.assertIsNone(ci_gate.pull_request_commits("token", repository, 12, 1))
        self.assertIsNone(ci_gate.pull_request_commits("token", repository, 12, 250))
        with mock.patch.object(ci_gate, "github_request", return_value={"errors": ["invalid"]}):
            self.assertIsNone(ci_gate.pull_request_commits("token", repository, 12, 1))
        missing_cursor = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "totalCount": 1,
                            "nodes": [signed_commit()],
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                        }
                    }
                }
            }
        }
        with mock.patch.object(ci_gate, "github_request", return_value=missing_cursor):
            self.assertIsNone(ci_gate.pull_request_commits("token", repository, 12, 1))

    def test_creates_a_missing_copy_ref_and_updates_an_existing_ref(self):
        not_found = github_app.GitHubRequestError("GET", "/ref", 404, "missing")
        with mock.patch.object(
            ci_gate,
            "github_request",
            side_effect=[
                not_found,
                {"ref": "created"},
                {"object": {"sha": full_sha}},
                {"ref": "updated"},
            ],
        ) as github_request:
            ci_gate.set_copied_revision("token", repository, 12, full_sha)
            ci_gate.set_copied_revision("token", repository, 12, new_sha)

        self.assertEqual(
            github_request.call_args_list,
            [
                mock.call(
                    f"/repos/{repository}/git/ref/heads/pull-request/12",
                    "token",
                ),
                mock.call(
                    f"/repos/{repository}/git/refs",
                    "token",
                    "POST",
                    {"ref": "refs/heads/pull-request/12", "sha": full_sha},
                ),
                mock.call(
                    f"/repos/{repository}/git/ref/heads/pull-request/12",
                    "token",
                ),
                mock.call(
                    f"/repos/{repository}/git/refs/heads/pull-request/12",
                    "token",
                    "PATCH",
                    {"sha": new_sha, "force": True},
                ),
            ],
        )

    def test_does_not_republish_an_identical_copy_ref(self):
        with mock.patch.object(
            ci_gate,
            "github_request",
            return_value={"object": {"sha": full_sha}},
        ) as github_request:
            ci_gate.set_copied_revision("token", repository, 12, full_sha)

        github_request.assert_called_once_with(
            f"/repos/{repository}/git/ref/heads/pull-request/12",
            "token",
        )

    def test_ignores_a_missing_copy_ref_during_deletion(self):
        not_found = github_app.GitHubRequestError("DELETE", "/ref", 404, "missing")
        with mock.patch.object(ci_gate, "github_request", side_effect=not_found):
            ci_gate.delete_copied_revision("token", repository, 12)

        failure = github_app.GitHubRequestError("DELETE", "/ref", 403, "denied")
        with (
            mock.patch.object(ci_gate, "github_request", side_effect=failure),
            self.assertRaises(github_app.GitHubRequestError),
        ):
            ci_gate.delete_copied_revision("token", repository, 12)

    def test_posts_comments_and_checks_maintainer_permission(self):
        with mock.patch.object(
            ci_gate,
            "github_request",
            side_effect=[
                None,
                {"permission": "write"},
                {"permission": "maintain"},
                {"permission": "read"},
            ],
        ) as github_request:
            ci_gate.comment("token", repository, 12, "message")
            self.assertTrue(ci_gate.approver_can_run_ci("token", repository, "griwes"))
            self.assertTrue(ci_gate.approver_can_run_ci("token", repository, "maintainer"))
            self.assertFalse(ci_gate.approver_can_run_ci("token", repository, "reader"))
        self.assertEqual(github_request.call_count, 4)
        self.assertFalse(ci_gate.approver_can_run_ci("token", repository, None))

        not_found = github_app.GitHubRequestError("GET", "/permission", 404, "missing")
        with mock.patch.object(ci_gate, "github_request", side_effect=not_found):
            self.assertFalse(ci_gate.approver_can_run_ci("token", repository, "external"))

    def test_automatically_copies_an_allowlisted_local_actor(self):
        pr = pull_request(actor="griwes")
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(ci_gate, "pull_request_commits", return_value=[signed_commit()]),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=pr),
                "token",
                repository,
            )

        self.assertIn("automatically approved", result)
        copy.assert_called_once_with("token", repository, 12, full_sha)

    def test_unsigned_automatic_revision_requires_exact_approval(self):
        pr = pull_request(actor="griwes")
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(
                ci_gate, "pull_request_commits", return_value=[{"commit": {"oid": full_sha}}]
            ),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
            mock.patch.object(ci_gate, "delete_copied_revision") as delete,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=pr), "token", repository
            )
        self.assertEqual(result, "revision requires exact approval")
        copy.assert_not_called()
        delete.assert_called_once_with("token", repository, 12)

    def test_signed_trusted_fork_revision_is_copied_automatically(self):
        pr = pull_request(actor="griwes", head_repository="griwes/reaveros")
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(ci_gate, "pull_request_commits", return_value=[signed_commit()]),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=pr), "token", repository
            )
        self.assertIn("automatically approved", result)
        copy.assert_called_once_with("token", repository, 12, full_sha)

    def test_github_signed_maintenance_commit_requires_the_bot_author(self):
        bot = "reaver-project-maintenance[bot]"
        pr = pull_request(actor=bot)
        commit = signed_commit(actor=bot)
        commit["commit"]["signature"]["signer"]["login"] = "web-flow"
        verified_author = {
            "author": {"login": bot, "type": "Bot"},
            "commit": {"verification": {"verified": True}},
        }
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(ci_gate, "pull_request_commits", return_value=[commit]),
            mock.patch.object(ci_gate, "github_request", return_value=verified_author),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=pr), "token", repository
            )
        self.assertIn("automatically approved", result)
        copy.assert_called_once_with("token", repository, 12, full_sha)

        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(ci_gate, "pull_request_commits", return_value=[commit]),
            mock.patch.object(ci_gate, "github_request", return_value={"author": None}),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
            mock.patch.object(ci_gate, "delete_copied_revision") as delete,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=pr), "token", repository
            )
        self.assertEqual(result, "revision requires exact approval")
        copy.assert_not_called()
        delete.assert_called_once_with("token", repository, 12)

        with mock.patch.object(ci_gate, "github_request") as request:
            self.assertEqual(
                ci_gate.github_signed_bot_commits(
                    "token",
                    repository,
                    [
                        signed_commit(actor=bot),
                        {"commit": {"oid": "bad"}},
                        {"commit": {"oid": "bad", "signature": {"signer": {"login": "web-flow"}}}},
                    ],
                    bot,
                ),
                set(),
            )
        request.assert_not_called()

    def test_stale_pull_request_event_cannot_replace_the_copy(self):
        current = pull_request(sha=new_sha, actor="griwes")
        stale = pull_request(sha=full_sha, actor="griwes")
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=current),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=stale),
                "token",
                repository,
            )

        self.assertEqual(result, "ignored stale pull request event")
        copy.assert_not_called()

    def test_external_revision_removes_the_old_copy_and_requests_approval(self):
        pr = pull_request(head_repository="external/reaveros")
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(ci_gate, "delete_copied_revision") as delete,
            mock.patch.object(ci_gate, "comment") as comment,
        ):
            result = ci_gate.handle_pull_request(
                event_payload(pr=pr),
                "token",
                repository,
            )

        self.assertEqual(result, "revision requires exact approval")
        delete.assert_called_once_with("token", repository, 12)
        self.assertIn(full_sha[:12], comment.call_args.args[3])

    def test_closing_a_pull_request_removes_its_copy(self):
        with mock.patch.object(ci_gate, "delete_copied_revision") as delete:
            result = ci_gate.handle_pull_request(
                event_payload(action="closed"),
                "token",
                repository,
            )
        self.assertEqual(result, "removed copied revision")
        delete.assert_called_once_with("token", repository, 12)

    def test_ignores_unrelated_and_ineligible_pull_request_events(self):
        with mock.patch.object(ci_gate, "delete_copied_revision") as delete:
            self.assertEqual(
                ci_gate.handle_pull_request(event_payload(action="assigned"), "token", repository),
                "ignored pull request action",
            )

        draft = {**pull_request(), "draft": True}
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=draft),
            mock.patch.object(ci_gate, "delete_copied_revision") as delete,
        ):
            self.assertEqual(
                ci_gate.handle_pull_request(
                    event_payload(action="synchronize", pr=draft),
                    "token",
                    repository,
                ),
                "pull request is not eligible",
            )
        delete.assert_called_once()

    def test_external_synchronize_does_not_repeat_the_instruction_comment(self):
        pr = pull_request(head_repository="external/reaveros")
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pr),
            mock.patch.object(ci_gate, "delete_copied_revision"),
            mock.patch.object(ci_gate, "comment") as comment,
        ):
            ci_gate.handle_pull_request(
                event_payload(action="synchronize", pr=pr),
                "token",
                repository,
            )
        comment.assert_not_called()

    def test_exact_abbreviated_approval_copies_the_resolved_full_revision(self):
        payload = {
            "action": "created",
            "comment": {"body": "/ok to test 0123456789ab", "user": {"login": "griwes"}},
            "issue": {"number": 12, "pull_request": {"url": "example"}},
        }
        with (
            mock.patch.object(ci_gate, "approver_can_run_ci", return_value=True),
            mock.patch.object(ci_gate, "resolve_revision", return_value=full_sha),
            mock.patch.object(ci_gate, "pull_request", return_value=pull_request()),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
            mock.patch.object(ci_gate, "comment"),
        ):
            result = ci_gate.handle_issue_comment(payload, "token", repository)

        self.assertEqual(result, f"copied explicitly approved revision {full_sha}")
        copy.assert_called_once_with("token", repository, 12, full_sha)

    def test_stale_or_ambiguous_approval_is_refused(self):
        payload = {
            "action": "created",
            "comment": {"body": "/ok to test 0123456", "user": {"login": "griwes"}},
            "issue": {"number": 12, "pull_request": {"url": "example"}},
        }
        with (
            mock.patch.object(ci_gate, "approver_can_run_ci", return_value=True),
            mock.patch.object(ci_gate, "resolve_revision", return_value=None),
            mock.patch.object(ci_gate, "pull_request", return_value=pull_request()),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
            mock.patch.object(ci_gate, "comment") as comment,
        ):
            result = ci_gate.handle_issue_comment(payload, "token", repository)

        self.assertEqual(result, "approval does not match current revision")
        copy.assert_not_called()
        self.assertIn("Refused stale", comment.call_args.args[3])

    def test_nonmaintainer_cannot_approve_ci(self):
        payload = {
            "action": "created",
            "comment": {"body": "/ok to test 0123456", "user": {"login": "external"}},
            "issue": {"number": 12, "pull_request": {"url": "example"}},
        }
        with (
            mock.patch.object(ci_gate, "approver_can_run_ci", return_value=False),
            mock.patch.object(ci_gate, "set_copied_revision") as copy,
            mock.patch.object(ci_gate, "comment") as comment,
        ):
            result = ci_gate.handle_issue_comment(payload, "token", repository)

        self.assertEqual(result, "commenter cannot approve CI")
        copy.assert_not_called()
        self.assertIn("maintainer", comment.call_args.args[3])

    def test_ignores_unrelated_comments_and_explains_malformed_approvals(self):
        payload = {
            "action": "created",
            "comment": {"body": "looks good", "user": {"login": "griwes"}},
            "issue": {"number": 12, "pull_request": {"url": "example"}},
        }
        with mock.patch.object(ci_gate, "comment") as comment:
            self.assertEqual(
                ci_gate.handle_issue_comment(payload, "token", repository),
                "ignored non-approval comment",
            )
        comment.assert_not_called()

        payload["comment"]["body"] = "/ok to test main"
        with (
            mock.patch.object(ci_gate, "pull_request", return_value=pull_request()),
            mock.patch.object(ci_gate, "comment") as comment,
        ):
            self.assertEqual(
                ci_gate.handle_issue_comment(payload, "token", repository),
                "ignored non-approval comment",
            )
        self.assertIn(full_sha[:12], comment.call_args.args[3])

        payload["action"] = "edited"
        self.assertEqual(
            ci_gate.handle_issue_comment(payload, "token", repository),
            "ignored comment action",
        )

    def test_handler_rejects_bad_signatures_before_requesting_a_token(self):
        body = json.dumps(event_payload()).encode()
        ci_gate.cached_credentials = {
            "app_id": "1",
            "app_slug": "reaver-project-ci-gate",
            "private_key": "key",
            "webhook_secret": "secret",
        }
        event = {
            "body": body.decode(),
            "headers": {
                "x-github-event": "pull_request",
                "x-hub-signature-256": "sha256=wrong",
            },
        }
        with mock.patch.object(ci_gate, "installation_token") as token:
            result = ci_gate.handler(event, None)
        self.assertEqual(result["statusCode"], 401)
        token.assert_not_called()

    def test_handler_processes_a_signed_webhook_for_an_allowed_repository(self):
        body = json.dumps(event_payload(action="closed"), separators=(",", ":")).encode()
        secret = "secret"
        signature = (
            "sha256="
            + hmac.new(
                secret.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
        )
        ci_gate.cached_credentials = {
            "app_id": "1",
            "app_slug": "reaver-project-ci-gate",
            "private_key": "key",
            "webhook_secret": secret,
        }
        event = {
            "body": body.decode(),
            "headers": {
                "x-github-event": "pull_request",
                "x-hub-signature-256": signature,
            },
        }
        with (
            mock.patch.object(ci_gate, "installation_token", return_value="token"),
            mock.patch.object(
                ci_gate,
                "handle_pull_request",
                return_value="removed copied revision",
            ) as handle,
        ):
            result = ci_gate.handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        handle.assert_called_once()
        self.assertEqual(json.loads(result["body"])["message"], "removed copied revision")

    def test_handler_accepts_ping_and_ignores_unsubscribed_events(self):
        secret = "secret"
        ci_gate.cached_credentials = {
            "app_id": "1",
            "app_slug": "reaver-project-ci-gate",
            "private_key": "key",
            "webhook_secret": secret,
        }

        def signed_event(event_name):
            body = b"{}"
            signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return {
                "body": body.decode(),
                "headers": {
                    "x-github-event": event_name,
                    "x-hub-signature-256": signature,
                },
            }

        self.assertEqual(ci_gate.handler(signed_event("ping"), None)["statusCode"], 200)
        result = ci_gate.handler(signed_event("push"), None)
        self.assertEqual(json.loads(result["body"])["message"], "ignored webhook event")

    def test_handler_routes_issue_comments_and_reports_bad_payloads(self):
        payload = {
            "action": "created",
            "comment": {"body": "hello", "user": {"login": "external"}},
            "installation": {"id": 17},
            "issue": {"number": 12, "pull_request": {"url": "example"}},
            "repository": {"full_name": repository, "id": 42},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        secret = "secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        ci_gate.cached_credentials = {
            "app_id": "1",
            "app_slug": "reaver-project-ci-gate",
            "private_key": "key",
            "webhook_secret": secret,
        }
        event = {
            "body": body.decode(),
            "headers": {
                "x-github-event": "issue_comment",
                "x-hub-signature-256": signature,
            },
        }
        with (
            mock.patch.object(ci_gate, "installation_token", return_value="token"),
            mock.patch.object(
                ci_gate,
                "handle_issue_comment",
                return_value="ignored non-approval comment",
            ) as handle,
        ):
            result = ci_gate.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        handle.assert_called_once()

        bad = {"body": "missing repository", "headers": {}}
        result = ci_gate.handler(bad, None)
        self.assertIn(result["statusCode"], {400, 401})


if __name__ == "__main__":
    unittest.main()
