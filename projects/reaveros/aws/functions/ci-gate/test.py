import base64
import hashlib
import hmac
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import lib  # noqa: E402

sha = "0123456789abcdef0123456789abcdef01234567"


class CiGateLibraryTests(unittest.TestCase):
    def test_reads_plain_and_base64_event_bodies(self):
        self.assertEqual(lib.event_body({"body": "hello"}), b"hello")
        self.assertEqual(
            lib.event_body(
                {
                    "body": base64.b64encode(b"hello").decode(),
                    "isBase64Encoded": True,
                }
            ),
            b"hello",
        )
        with self.assertRaisesRegex(ValueError, "valid base64"):
            lib.event_body({"body": "%%%", "isBase64Encoded": True})
        with self.assertRaisesRegex(ValueError, "too large"):
            lib.event_body({"body": "hello"}, maximum_size=4)
        with self.assertRaisesRegex(ValueError, "missing"):
            lib.event_body({})

    def test_reads_headers_case_insensitively_and_verifies_signatures(self):
        body = b"payload"
        signature = (
            "sha256="
            + hmac.new(
                b"secret",
                body,
                hashlib.sha256,
            ).hexdigest()
        )
        event = {"headers": {"X-Hub-Signature-256": signature}}

        self.assertEqual(lib.event_header(event, "x-hub-signature-256"), signature)
        self.assertTrue(lib.verify_signature(body, signature, "secret"))
        self.assertFalse(lib.verify_signature(body, signature, "different"))
        self.assertEqual(lib.event_header({}, "missing"), "")
        with self.assertRaisesRegex(ValueError, "secret is missing"):
            lib.verify_signature(body, signature, "")

    def test_parses_only_object_payloads(self):
        self.assertEqual(lib.parse_payload(b'{"answer":42}'), {"answer": 42})
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            lib.parse_payload(b"{")
        with self.assertRaisesRegex(ValueError, "object"):
            lib.parse_payload(b"[]")

    def test_accepts_github_sized_commit_abbreviations(self):
        self.assertEqual(lib.approval_sha("/ok to test 0123456"), "0123456")
        self.assertEqual(lib.approval_sha(f" /ok to test {sha} \n"), sha)
        self.assertIsNone(lib.approval_sha("/ok to test 012345"))
        self.assertIsNone(lib.approval_sha(f"/ok to test {sha}0"))
        self.assertIsNone(lib.approval_sha("please /ok to test 0123456"))
        self.assertIsNone(lib.approval_sha(None))
        self.assertTrue(lib.starts_with_approval_command(" /ok to test nope"))

    def test_extracts_and_restricts_repository_identity(self):
        payload = {
            "repository": {"full_name": "reaver-project/reaveros", "id": 42},
            "installation": {"id": 17},
        }
        self.assertEqual(
            lib.repository_identity(payload, {"reaver-project/reaveros"}),
            ("reaver-project/reaveros", 42),
        )
        self.assertEqual(lib.installation_id(payload), 17)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            lib.repository_identity(payload, {"someone/else"})
        with self.assertRaisesRegex(ValueError, "repository is missing"):
            lib.repository_identity({}, {"reaver-project/reaveros"})
        with self.assertRaisesRegex(ValueError, "installation is missing"):
            lib.installation_id({})

    def test_extracts_pull_request_numbers_from_both_event_shapes(self):
        self.assertEqual(lib.pull_request_number({"pull_request": {"number": 3}}), 3)
        self.assertEqual(
            lib.pull_request_number({"issue": {"number": 4, "pull_request": {"url": "example"}}}),
            4,
        )
        with self.assertRaisesRegex(ValueError, "does not describe"):
            lib.pull_request_number({"issue": {"number": 4}})
        with self.assertRaisesRegex(ValueError, "number is missing"):
            lib.pull_request_number({"pull_request": {"number": 0}})

    def test_selects_only_open_nondraft_revisions(self):
        pull_request = {
            "state": "open",
            "draft": False,
            "head": {"sha": sha},
        }
        self.assertEqual(lib.current_revision(pull_request), sha)
        self.assertIsNone(lib.current_revision({**pull_request, "draft": True}))
        self.assertIsNone(lib.current_revision({**pull_request, "state": "closed"}))
        with self.assertRaisesRegex(ValueError, "head SHA"):
            lib.current_revision({**pull_request, "head": {"sha": "short"}})

    def test_automatic_revisions_require_an_allowed_actor_and_local_branch(self):
        pull_request = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": sha,
                "repo": {"full_name": "reaver-project/reaveros"},
            },
            "user": {"login": "Griwes"},
        }
        self.assertEqual(
            lib.automatic_revision(
                pull_request,
                "reaver-project/reaveros",
                {"griwes"},
            ),
            sha,
        )
        self.assertIsNone(lib.automatic_revision(pull_request, "reaver-project/reaveros", set()))
        pull_request["head"]["repo"]["full_name"] = "fork/reaveros"
        self.assertIsNone(
            lib.automatic_revision(
                pull_request,
                "reaver-project/reaveros",
                {"griwes"},
            )
        )
        self.assertIsNone(
            lib.automatic_revision(
                {**pull_request, "draft": True},
                "reaver-project/reaveros",
                {"griwes"},
            )
        )

    def test_automatic_admission_requires_a_verified_linear_commit_chain(self):
        parent = "a" * 40
        head = "b" * 40

        def node(oid, parent_oid, signer="griwes", author="griwes", valid=True):
            return {
                "commit": {
                    "oid": oid,
                    "parents": {"nodes": [{"oid": parent_oid}]},
                    "signature": {"isValid": valid, "signer": {"login": signer}},
                    "author": {"user": {"login": author}},
                }
            }

        commits = [node(parent, "0" * 40), node(head, parent)]
        self.assertTrue(lib.signed_commit_chain(commits, 2, head, "griwes"))
        self.assertFalse(lib.signed_commit_chain(commits, 3, head, "griwes"))
        self.assertFalse(lib.signed_commit_chain(commits, 250, head, "griwes"))
        self.assertFalse(lib.signed_commit_chain(commits, 2, parent, "griwes"))
        self.assertFalse(
            lib.signed_commit_chain([commits[0], node(head, "0" * 40)], 2, head, "griwes")
        )
        self.assertFalse(
            lib.signed_commit_chain(
                [commits[0], node(head, parent, valid=False)], 2, head, "griwes"
            )
        )
        self.assertFalse(
            lib.signed_commit_chain(
                [commits[0], node(head, parent, signer="other")], 2, head, "griwes"
            )
        )
        self.assertFalse(lib.signed_commit_chain([{"commit": {"oid": head}}], 1, head, "griwes"))

        bot = "reaver-project-maintenance[bot]"
        self.assertTrue(
            lib.signed_commit_chain([node(head, parent, "web-flow", bot)], 1, head, bot)
        )
        self.assertFalse(
            lib.signed_commit_chain([node(head, parent, "web-flow", "other")], 1, head, bot)
        )

    def test_constructs_only_numeric_copy_branches(self):
        self.assertEqual(lib.copied_branch(12), "pull-request/12")
        with self.assertRaisesRegex(ValueError, "invalid"):
            lib.copied_branch(0)


if __name__ == "__main__":
    unittest.main()
