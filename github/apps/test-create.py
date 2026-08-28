import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

module_path = Path(__file__).with_name("create")
loader = importlib.machinery.SourceFileLoader("create_app", str(module_path))
specification = importlib.util.spec_from_loader("create_app", loader)
create_app = importlib.util.module_from_spec(specification)
specification.loader.exec_module(create_app)


def decrypt(path, passphrase):
    gpg_home = tempfile.mkdtemp(prefix="reaver-project-gpg-", dir="/dev/shm")
    reader, writer = os.pipe()
    try:
        os.write(writer, passphrase.encode())
    finally:
        os.close(writer)
    try:
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-options",
                "--homedir",
                gpg_home,
                "--pinentry-mode",
                "loopback",
                "--no-symkey-cache",
                "--passphrase-fd",
                str(reader),
                "--decrypt",
                str(path),
            ],
            check=True,
            capture_output=True,
            pass_fds=(reader,),
        )
        return json.loads(result.stdout)
    finally:
        os.close(reader)
        shutil.rmtree(gpg_home)


class CreateAppTests(unittest.TestCase):
    def test_activates_a_supplied_https_webhook(self):
        manifest = create_app.configured_manifest(
            {"hook_attributes": {"active": False}},
            "https://example.lambda-url.us-west-2.on.aws/",
        )
        self.assertEqual(
            manifest["hook_attributes"],
            {
                "active": True,
                "url": "https://example.lambda-url.us-west-2.on.aws/",
            },
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            create_app.configured_manifest({}, "http://example.com/webhook")

    def test_encrypts_credentials_only_on_memory_backed_storage(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
            os.chmod(directory, 0o700)
            output = Path(directory) / "credentials.json.gpg"
            credentials = {"id": 1234, "pem": "test-private-key"}
            create_app.encrypt_credentials(output, credentials, "test-passphrase")

            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b"test-private-key", output.read_bytes())
            self.assertEqual(decrypt(output, "test-passphrase"), credentials)

    def test_rejects_persistent_storage(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository) as directory:
            os.chmod(directory, 0o700)
            if create_app.memory_filesystem(directory):
                self.skipTest("system temporary directory is memory-backed")
            with self.assertRaises(ValueError):
                create_app.encrypt_credentials(
                    Path(directory) / "credentials.json.gpg",
                    {"id": 1234, "pem": "test-private-key"},
                    "test-passphrase",
                )


if __name__ == "__main__":
    unittest.main()
