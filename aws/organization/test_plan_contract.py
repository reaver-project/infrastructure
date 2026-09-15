#!/usr/bin/env python3

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaver_project_aws import plan_contract


class PlanContractTests(unittest.TestCase):
    def test_private_plan_round_trip(self):
        plan = {"schema_version": 1, "state": {"actions": []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            plan_contract.write(path, plan)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(plan_contract.read(path), plan)
            with self.assertRaises(FileExistsError):
                plan_contract.write(path, plan)

    def test_rejects_changed_plan(self):
        with self.assertRaises(SystemExit):
            plan_contract.require_match(
                {"repository_revision": "old"},
                {"repository_revision": "new"},
            )

    def test_rejects_dirty_repository(self):
        with (
            patch.object(plan_contract, "git", return_value=" M config.json"),
            self.assertRaises(SystemExit),
        ):
            plan_contract.repository_identity()

    def test_plan_is_stable_json(self):
        plan = {"b": 2, "a": 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            plan_contract.write(path, plan)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), plan)

    def test_rejects_a_public_plan_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(SystemExit):
                plan_contract.read(path)

    def test_rejects_a_symbolic_link(self):
        with tempfile.TemporaryDirectory() as directory:
            private_plan = Path(directory) / "private.json"
            private_plan.write_text("{}", encoding="utf-8")
            private_plan.chmod(stat.S_IRUSR | stat.S_IWUSR)
            link = Path(directory) / "link.json"
            link.symlink_to(private_plan)
            with self.assertRaises(SystemExit):
                plan_contract.read(link)


if __name__ == "__main__":
    unittest.main()
