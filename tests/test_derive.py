"""Local derivative registration: provenance, independent review, and no sends."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anigc.cli import main
from anigc.task_store import StoreError, TaskStore
from test_task_store import PNG


class DeriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore.create(self.root / "task", "仅离线测试本地派生登记。", "派生登记测试", limit=4)
        self.round = self.store.prepare("离线输入", "离线参考", "fixture", "fixture")
        self.image = self.root / "cropped.png"
        self.derived_bytes = PNG + b"offline-derived-fixture"
        self.image.write_bytes(self.derived_bytes)
        self.note = "仅离线模拟：中心裁边；保留原始 API 图片。"

    def returned(self, complete=True):
        self.store.reserve(self.round, evidence_ready=True)
        self.store.stage_result(self.round, [(PNG, ".png")], "离线模拟响应", complete=complete)
        self.store.collect(self.round)

    def derived(self):
        return self.store.derive(self.round, "output-01.png", self.image, self.note)

    def pass_review(self, candidate):
        self.store.review(self.round, candidate, "仅离线预设监修结论。", "pass")

    def test_derivative_keeps_receipt_and_requires_independent_review(self):
        self.returned()
        self.pass_review("output-01.png")
        receipt = self.store.snapshot()["attempts"][0]["receipt"]
        candidate = self.derived()
        self.assertEqual(candidate, "derived-001.png")
        attempt = self.store.snapshot()["attempts"][0]
        self.assertEqual(attempt["receipt"], receipt)
        self.assertEqual(attempt["outputs"][0], receipt["outputs"][0])
        derivative = attempt["outputs"][1]
        self.assertEqual(derivative["derived_from"], receipt["outputs"][0])
        self.assertIn(self.note, (self.store.path / derivative["transformation"]["path"]).read_text())
        self.assertEqual((self.store.path / receipt["outputs"][0]["path"]).read_bytes(), PNG)
        self.assertEqual(self.store.recover()["remaining"], 3)
        with self.assertRaises(StoreError):
            self.store.promote(self.round, candidate)
        self.pass_review(candidate)
        final = self.store.promote(self.round, candidate)
        self.assertEqual((self.store.path / final).read_bytes(), self.derived_bytes)
        self.assertEqual(len(self.store.collect(self.round)["outputs"]), 2)
        self.assertEqual(self.store.recover()["remaining"], 3)
        self.assertEqual(self.store.recover()["warnings"], [])

    def test_changed_original_invalidates_derived_delivery(self):
        self.returned()
        candidate = self.derived()
        self.pass_review(candidate)
        final = self.store.promote(self.round, candidate)
        original = self.store.path / "rounds" / self.round / "output-01.png"
        original.write_bytes(PNG + b"changed-original")
        with self.assertRaises(StoreError):
            self.store.promote(self.round, candidate)
        with self.assertRaises(StoreError):
            self.store.accept(final, "accepted", "不能接受来源已变动的版本。")
        self.assertTrue(any("output-01.png" in item for item in self.store.recover()["warnings"]))
        self.assertIn("旧通过记录不可沿用", self.store.preview().read_text())
        self.assertEqual((self.store.path / final).read_bytes(), self.derived_bytes)

    def test_changed_transformation_invalidates_review_and_delivery(self):
        self.returned()
        candidate = self.derived()
        self.pass_review(candidate)
        final = self.store.promote(self.round, candidate)
        record = self.store.snapshot()["attempts"][0]["outputs"][1]
        note_path = self.store.path / record["transformation"]["path"]
        note_path.write_text("处理说明已经变更。")
        with self.assertRaises(StoreError):
            self.pass_review(candidate)
        with self.assertRaises(StoreError):
            self.store.promote(self.round, candidate)
        with self.assertRaises(StoreError):
            self.store.accept(final, "accepted", "不能接受依据已变动的版本。")
        self.assertTrue(any(note_path.name in item for item in self.store.recover()["warnings"]))
        self.assertIn("旧通过记录不可沿用", self.store.preview().read_text())

    def test_no_successful_source_or_damaged_source_cannot_register(self):
        with self.assertRaises(StoreError):
            self.derived()
        self.assertEqual(self.store.recover()["remaining"], 4)
        self.returned()
        original = self.store.path / "rounds" / self.round / "output-01.png"
        original.write_bytes(PNG + b"changed")
        with self.assertRaises(StoreError):
            self.derived()
        self.assertEqual(len(self.store.snapshot()["attempts"][0]["outputs"]), 1)
        self.assertFalse(list(original.parent.glob("derived-*")))

    def test_incomplete_response_cannot_be_made_deliverable_by_deriving(self):
        self.returned(complete=False)
        candidate = self.derived()
        self.pass_review(candidate)
        with self.assertRaises(StoreError):
            self.store.promote(self.round, candidate)
        self.assertEqual(self.store.recover()["remaining"], 3)

    def test_interrupted_registration_resumes_same_bytes_without_overwrite(self):
        self.returned()
        with patch.object(self.store, "_save", side_effect=OSError("模拟状态写入中断")):
            with self.assertRaises(OSError):
                self.derived()
        saved = self.store.path / "rounds" / self.round / "derived-001.png"
        before = (saved.stat().st_ino, saved.stat().st_mtime_ns)
        self.assertTrue(any(saved.name in item for item in self.store.recover()["warnings"]))
        self.image.write_bytes(PNG + b"different-derived-image")
        with self.assertRaises(StoreError):
            self.derived()
        self.assertEqual(saved.read_bytes(), self.derived_bytes)
        self.image.write_bytes(self.derived_bytes)
        candidate = self.derived()
        self.assertEqual(candidate, self.derived())
        self.assertEqual((saved.stat().st_ino, saved.stat().st_mtime_ns), before)
        self.assertEqual(len(self.store.snapshot()["attempts"][0]["outputs"]), 2)
        self.assertEqual(self.store.recover()["remaining"], 3)

    def test_cli_registers_only_local_derivative(self):
        self.returned()
        note = self.root / "crop-note.md"
        note.write_text(self.note)
        output = io.StringIO()
        with patch("anigc.cli.generate_round", side_effect=AssertionError("must not send")) as send:
            with redirect_stdout(output):
                code = main(["derive", str(self.store.path), self.round, "output-01.png", "--image", str(self.image), "--note-file", str(note)])
        self.assertEqual(code, 0)
        self.assertIn("derived-001.png", output.getvalue())
        send.assert_not_called()
        self.assertEqual(self.store.recover()["remaining"], 3)


if __name__ == "__main__":
    unittest.main()
