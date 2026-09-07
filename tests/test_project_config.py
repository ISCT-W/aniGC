"""Project isolation stays enforced after removing deployment identifiers."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anigc.config import reference_project
from anigc.task_store import StoreError, TaskStore


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_offline_never_reads_real_project_configuration(self):
        with patch("anigc.config.read_settings", side_effect=AssertionError("no config read")):
            task = TaskStore.create(self.root / "offline", "Synthetic test", "Offline")
            self.assertEqual(task.snapshot()["project_id"], "offline-fixture")

    def test_generation_requires_local_project_before_creating_files(self):
        with patch("anigc.config.read_settings", return_value={}):
            with self.assertRaises(StoreError):
                TaskStore.create(self.root / "missing", "Synthetic test", "Missing",
                                 mode="generation", authorization="Fixture authorization; no API calls")
        self.assertFalse((self.root / "missing").exists())

    def test_configured_project_is_bound_and_different_project_is_rejected(self):
        with patch("anigc.config.read_settings", return_value={"ANIGC_REFERENCE_PROJECT_ID": "project-a"}):
            task = TaskStore.create(self.root / "valid", "Synthetic test", "Configured",
                                    mode="generation", authorization="Fixture authorization; no API calls")
            self.assertEqual(task.snapshot()["project_id"], "project-a")
            with self.assertRaises(StoreError):
                TaskStore.create(self.root / "other", "Synthetic test", "Other",
                                 mode="generation", authorization="Fixture", project_id="project-b")
        self.assertFalse((self.root / "other").exists())
        before = (task.path / "state.json").read_bytes()
        with patch("anigc.config.read_settings", return_value={"ANIGC_REFERENCE_PROJECT_ID": "project-b"}):
            with self.assertRaises(StoreError):
                task.recover()
        self.assertEqual((task.path / "state.json").read_bytes(), before)
        self.assertEqual(json.loads(before)["project_id"], "project-a")

    def test_project_reads_selected_setting_with_environment_precedence(self):
        config = self.root / ".env"
        config.write_text('ANIGC_REFERENCE_PROJECT_ID=project-a\nUNRELATED="unterminated\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(reference_project("generation", config), "project-a")
        with patch.dict(os.environ, {"ANIGC_REFERENCE_PROJECT_ID": "project-b"}, clear=True):
            self.assertEqual(reference_project("generation", config), "project-b")

    def test_invalid_project_does_not_appear_in_error(self):
        with patch("anigc.config.read_settings", return_value={"ANIGC_REFERENCE_PROJECT_ID": "https://private.invalid/?token=value"}):
            with self.assertRaises(StoreError) as caught:
                reference_project("generation")
        self.assertNotIn("private.invalid", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
