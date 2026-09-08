"""Offline behavioral tests for task records, budgets, and review-gated delivery."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anigc.task_store import StoreError, TaskStore  # noqa: E402


# A fixed, tiny PNG fixture. Tests never contact a model or a remote service.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA1sAAAAASUVORK5CYII="
)


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task_dir = self.root / "task"
        TaskStore.create(
            self.task_dir,
            brief="仅使用本地固定样本测试记录流程，不调用生成服务。",
            title="离线任务测试",
            mode="offline",
            limit=4,
        )
        self.store = TaskStore(self.task_dir)
        self.fixture = self.root / "fixture.png"
        self.fixture.write_bytes(PNG)

    def prepare(self) -> str:
        return self.store.prepare(
            prompt="固定离线样本；不提交到任何生成服务。",
            reference="离线测试占位依据，不能作为角色设定或远端资料。",
            backend="fixture",
            model="fixture",
        )

    def sent_failure(self) -> str:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        self.store.finish(round_id, status="failed", note="离线模拟：请求已发送后失败。")
        return round_id

    def succeeded(self) -> str:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        self.store.finish(
            round_id,
            status="succeeded",
            outputs=[self.fixture],
            note="离线模拟：导入固定本地样本。",
        )
        return round_id

    def passed(self) -> str:
        round_id = self.succeeded()
        self.store.review(
            round_id,
            candidate="output-01.png",
            report="离线测试结论，仅用于验证交付门槛；不代表真实图片质量。",
            verdict="pass",
            blockers=[],
            unknowns=[],
        )
        return round_id

    def test_local_preparation_does_not_spend_budget(self) -> None:
        for _ in range(5):
            self.prepare()
        recovery = self.store.recover()
        self.assertEqual(recovery["remaining"], 4)
        self.assertEqual(recovery["unresolved"], [])

    def test_missing_evidence_rejects_send_without_spending(self) -> None:
        round_id = self.prepare()
        with self.assertRaises(StoreError):
            self.store.reserve(round_id, evidence_ready=False)
        self.assertEqual(self.store.recover()["remaining"], 4)
        self.store.reserve(round_id, evidence_ready=True)
        self.assertEqual(self.store.recover()["remaining"], 3)

    def test_four_sent_failures_exhaust_budget_and_fifth_is_rejected(self) -> None:
        for expected_remaining in (3, 2, 1, 0):
            self.sent_failure()
            self.assertEqual(self.store.recover()["remaining"], expected_remaining)
        fifth = self.prepare()
        with self.assertRaises(StoreError):
            self.store.reserve(fifth, evidence_ready=True)
        self.assertEqual(self.store.recover()["remaining"], 0)

    def test_default_task_allows_six_requests_and_rejects_seventh(self) -> None:
        store = TaskStore.create(self.root / "default-limit", "仅离线模拟默认次数。", "默认次数测试")
        self.assertEqual(store.snapshot()["batches"][0]["limit"], 6)
        for remaining in range(5, -1, -1):
            round_id = store.prepare("离线输入", "离线依据", "fixture", "fixture")
            store.reserve(round_id, evidence_ready=True)
            store.finish(round_id, status="failed", note="离线模拟已发送失败，不调用任何服务。")
            self.assertEqual(store.recover()["remaining"], remaining)
        seventh = store.prepare("第七次离线输入", "离线依据", "fixture", "fixture")
        with self.assertRaises(StoreError):
            store.reserve(seventh, evidence_ready=True)

    def test_saved_four_request_batch_is_not_expanded_on_recovery(self) -> None:
        self.sent_failure()
        state_path = self.task_dir / "state.json"
        before = state_path.read_bytes()
        resumed = TaskStore(self.task_dir)
        resumed.recover()
        resumed.preview()
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(resumed.snapshot()["batches"][0]["limit"], 4)
        self.assertEqual(resumed.recover()["remaining"], 3)

    def test_reopening_task_does_not_reset_budget(self) -> None:
        for _ in range(4):
            self.sent_failure()
        resumed = TaskStore(self.task_dir)
        self.assertEqual(resumed.recover()["remaining"], 0)
        with self.assertRaises(StoreError):
            resumed.reserve(self.prepare(), evidence_ready=True)

    def test_reserved_request_survives_recovery_and_blocks_another_send(self) -> None:
        first = self.prepare()
        self.store.reserve(first, evidence_ready=True)
        resumed = TaskStore(self.task_dir)
        recovered = resumed.recover()
        self.assertEqual(recovered["remaining"], 3)
        self.assertIn(first, recovered["unresolved"])
        with self.assertRaises(StoreError):
            resumed.reserve(self.prepare(), evidence_ready=True)

    def test_confirmed_not_sent_releases_reservation(self) -> None:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        self.store.finish(
            round_id,
            status="not_sent",
            note="离线模拟：发送前本地校验失败，确认未发出请求。",
        )
        self.assertEqual(self.store.recover()["remaining"], 4)
        self.assertEqual(self.store.recover()["unresolved"], [])
        for _ in range(4):
            self.sent_failure()
        self.assertEqual(self.store.recover()["remaining"], 0)

    def test_unknown_request_stays_counted_until_resolved(self) -> None:
        first = self.prepare()
        self.store.reserve(first, evidence_ready=True)
        self.store.finish(first, status="unknown", note="离线模拟：发送后超时，结果未知。")
        resumed = TaskStore(self.task_dir)
        self.assertIn(first, resumed.recover()["unresolved"])
        self.assertEqual(resumed.recover()["remaining"], 3)
        next_round = self.prepare()
        with self.assertRaises(StoreError):
            resumed.reserve(next_round, evidence_ready=True)
        with self.assertRaises(StoreError):
            resumed.finish(first, status="not_sent", note="不能把未知请求当成未发送。")
        resumed.finish(first, status="failed", note="离线模拟：查询原请求后确认失败。")
        resumed.reserve(next_round, evidence_ready=True)
        self.assertEqual(resumed.recover()["remaining"], 2)

    def test_unknown_can_resolve_to_success_without_second_charge(self) -> None:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        self.store.finish(round_id, status="unknown", note="离线模拟：等待原请求结果。")
        self.store.finish(
            round_id,
            status="succeeded",
            outputs=[self.fixture],
            note="离线模拟：取回原请求产物，无新增请求。",
        )
        self.assertEqual(self.store.recover()["remaining"], 3)
        self.assertEqual(self.store.recover()["unresolved"], [])
        self.assertEqual((self.task_dir / "rounds" / round_id / "output-01.png").read_bytes(), PNG)

    def test_concurrent_reservations_cannot_duplicate_one_request(self) -> None:
        round_id = self.prepare()
        barrier = threading.Barrier(2)

        def attempt() -> bool:
            concurrent_store = TaskStore(self.task_dir)
            barrier.wait(timeout=5)
            try:
                concurrent_store.reserve(round_id, evidence_ready=True)
            except StoreError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.store.recover()["remaining"], 3)
        self.assertEqual(self.store.recover()["unresolved"], [round_id])

    def test_successful_output_is_copied_and_cannot_be_overwritten(self) -> None:
        round_id = self.succeeded()
        saved = self.task_dir / "rounds" / round_id / "output-01.png"
        self.fixture.write_bytes(b"different bytes")
        self.assertEqual(saved.read_bytes(), PNG)
        with self.assertRaises(StoreError):
            self.store.finish(
                round_id,
                status="succeeded",
                outputs=[self.fixture],
                note="不得覆盖既有产物。",
            )
        self.assertEqual(saved.read_bytes(), PNG)

    def test_unreviewed_failed_and_unverified_candidates_cannot_be_final(self) -> None:
        round_id = self.succeeded()
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-01.png")
        for verdict in ("fail", "unverified"):
            with self.subTest(verdict=verdict):
                self.store.review(
                    round_id,
                    candidate="output-01.png",
                    report="离线测试：尚不满足交付条件。",
                    verdict=verdict,
                    blockers=["存在阻断问题"] if verdict == "fail" else [],
                    unknowns=["关键依据待确认"] if verdict == "unverified" else [],
                )
                with self.assertRaises(StoreError):
                    self.store.promote(round_id, candidate="output-01.png")
        self.assertFalse(list((self.task_dir / "final_output").glob("*.png")))

    def test_pass_cannot_ignore_blockers_or_unknowns(self) -> None:
        round_id = self.succeeded()
        for blockers, unknowns in ((["手部错误"], []), ([], ["角色版本未核实"])):
            with self.subTest(blockers=blockers, unknowns=unknowns):
                with self.assertRaises(StoreError):
                    self.store.review(
                        round_id,
                        candidate="output-01.png",
                        report="不能用通过标签绕过阻断或关键未知项。",
                        verdict="pass",
                        blockers=blockers,
                        unknowns=unknowns,
                    )
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-01.png")

    def test_changed_output_invalidates_previous_pass(self) -> None:
        round_id = self.passed()
        saved = self.task_dir / "rounds" / round_id / "output-01.png"
        saved.write_bytes(PNG + b"changed")
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-01.png")
        self.assertFalse(list((self.task_dir / "final_output").glob("*.png")))

    def test_changed_review_invalidates_previous_pass(self) -> None:
        round_id = self.passed()
        review_file = self.task_dir / "rounds" / round_id / "review.md"
        self.assertTrue(review_file.is_file())
        review_file.write_text("已修改的监修报告，不等于已登记报告。", encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-01.png")

    def test_latest_review_controls_delivery_and_old_report_is_preserved(self) -> None:
        round_id = self.passed()
        original = self.task_dir / "rounds" / round_id / "review.md"
        original_bytes = original.read_bytes()
        self.store.review(
            round_id,
            candidate="output-01.png",
            report="离线复核发现新问题。",
            verdict="fail",
            blockers=["离线测试阻断项"],
            unknowns=[],
        )
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertTrue((original.parent / "review-v002.md").is_file())
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-01.png")

    def test_user_rejection_preserves_delivered_file_and_acceptance_history(self) -> None:
        round_id = self.passed()
        relative_final = self.store.promote(round_id, candidate="output-01.png")
        final = self.task_dir / relative_final
        self.assertEqual(final.read_bytes(), PNG)
        self.store.accept(relative_final, status="accepted", comment="离线测试：接受此版本。")
        self.store.accept(relative_final, status="changes_requested", comment="离线测试：复核后需要修改。")
        self.assertEqual(final.read_bytes(), PNG)
        feedback = (self.task_dir / "feedback.md").read_text(encoding="utf-8")
        self.assertIn("离线测试：接受此版本。", feedback)
        self.assertIn("离线测试：复核后需要修改。", feedback)
        self.assertEqual(self.store.recover()["remaining"], 3)

    def test_explicit_new_batch_does_not_erase_prior_attempts(self) -> None:
        previous_rounds = [self.sent_failure() for _ in range(4)]
        with self.assertRaises(StoreError):
            self.store.new_batch(authorization="")
        self.store.new_batch(authorization="用户明确授权新的离线测试批次。")
        self.assertEqual(self.store.recover()["remaining"], 6)
        self.assertEqual([batch["limit"] for batch in self.store.snapshot()["batches"]], [4, 6])
        new_round = self.sent_failure()
        self.assertNotIn(new_round, previous_rounds)
        for round_id in previous_rounds:
            self.assertTrue((self.task_dir / "rounds" / round_id / "prompt.md").is_file())
        self.assertEqual(self.store.recover()["remaining"], 5)

    def test_unresolved_request_cannot_be_bypassed_with_new_batch(self) -> None:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        with self.assertRaises(StoreError):
            self.store.new_batch(authorization="不能绕过仍在执行的请求。")
        self.assertEqual(self.store.recover()["remaining"], 3)

    def test_changed_later_pass_blocks_acceptance_of_existing_delivery(self) -> None:
        round_id = self.passed()
        relative_final = self.store.promote(round_id, candidate="output-01.png")
        self.store.review(
            round_id,
            candidate="output-01.png",
            report="离线再次复核：登记第二份通过报告。",
            verdict="pass",
            blockers=[],
            unknowns=[],
        )
        later_report = self.task_dir / "rounds" / round_id / "review-v002.md"
        later_report.write_text("第二份报告已改变，旧通过结论不可沿用。", encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.accept(relative_final, status="accepted", comment="离线测试接受。")
        preview = self.store.preview().read_text(encoding="utf-8")
        self.assertNotIn("；文件一致", preview)
        self.assertIn("旧通过记录不可沿用", preview)
        self.assertEqual((self.task_dir / relative_final).read_bytes(), PNG)

    def test_interrupted_output_save_can_resume_without_refund_or_overwrite(self) -> None:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        with mock.patch.object(self.store, "_save", side_effect=OSError("模拟写状态时中断")):
            with self.assertRaises(OSError):
                self.store.finish(
                    round_id,
                    status="succeeded",
                    outputs=[self.fixture],
                    note="离线模拟：原图落盘后状态保存中断。",
                )
        orphan = self.task_dir / "rounds" / round_id / "output-01.png"
        self.assertEqual(orphan.read_bytes(), PNG)
        original_identity = (orphan.stat().st_ino, orphan.stat().st_mtime_ns)
        resumed = TaskStore(self.task_dir)
        recovered = resumed.recover()
        self.assertEqual(recovered["remaining"], 3)
        self.assertIn(round_id, recovered["unresolved"])
        self.assertTrue(any("output-01.png" in warning for warning in recovered["warnings"]))
        for invalid_status in ("not_sent", "failed"):
            with self.subTest(status=invalid_status):
                with self.assertRaises(StoreError):
                    resumed.finish(round_id, status=invalid_status, note="已有未登记原图，不能这样结束请求。")
        resumed.finish(
            round_id,
            status="succeeded",
            outputs=[self.fixture],
            note="离线模拟：重新登记同一份产物，不重新调用。",
        )
        self.assertEqual(resumed.recover()["remaining"], 3)
        self.assertEqual(resumed.recover()["unresolved"], [])
        self.assertEqual(orphan.read_bytes(), PNG)
        self.assertEqual((orphan.stat().st_ino, orphan.stat().st_mtime_ns), original_identity)

    def test_three_concurrent_rounds_cannot_exceed_last_budget_slot(self) -> None:
        for _ in range(3):
            self.sent_failure()
        rounds = [self.prepare() for _ in range(3)]
        barrier = threading.Barrier(3)

        def attempt(round_id: str) -> str | None:
            concurrent_store = TaskStore(self.task_dir)
            barrier.wait(timeout=5)
            try:
                concurrent_store.reserve(round_id, evidence_ready=True)
            except StoreError:
                return None
            return round_id

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(attempt, rounds))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.recover()["remaining"], 0)
        self.store.finish(winners[0], status="failed", note="离线模拟：唯一提交请求失败。")
        for round_id in rounds:
            if round_id != winners[0]:
                with self.assertRaises(StoreError):
                    self.store.reserve(round_id, evidence_ready=True)
        self.assertEqual(self.store.recover()["remaining"], 0)

    def test_changed_input_snapshots_block_sending_and_promotion(self) -> None:
        for stage in ("reserve", "promote"):
            for relative in ("prompt.md", "reference.md", "inputs/01-reference.png"):
                with self.subTest(stage=stage, relative=relative):
                    round_id = self.store.prepare(
                        prompt="离线完整输入。",
                        reference="离线参考快照。",
                        backend="fixture",
                        model="fixture",
                        inputs=[("reference", self.fixture)],
                    )
                    if stage == "promote":
                        self.store.reserve(round_id, evidence_ready=True)
                        self.store.finish(
                            round_id, status="succeeded", outputs=[self.fixture], note="离线保存固定样本。"
                        )
                        self.store.review(
                            round_id,
                            candidate="output-01.png",
                            report="离线通过记录。",
                            verdict="pass",
                            blockers=[],
                            unknowns=[],
                        )
                    snapshot = self.task_dir / "rounds" / round_id / relative
                    snapshot.write_bytes(snapshot.read_bytes() + b"\nchanged")
                    remaining = self.store.recover()["remaining"]
                    with self.assertRaises(StoreError):
                        if stage == "reserve":
                            self.store.reserve(round_id, evidence_ready=True)
                        else:
                            self.store.promote(round_id, candidate="output-01.png")
                    self.assertEqual(self.store.recover()["remaining"], remaining)
        self.assertFalse(list((self.task_dir / "final_output").glob("*.png")))

    def test_other_project_cannot_create_a_generation_reference_task(self) -> None:
        other_path = self.root / "other-project"
        with self.assertRaises(StoreError):
            TaskStore.create(
                other_path,
                brief="离线测试项目边界。",
                title="不得换用其他参考库",
                mode="offline",
                project_id="other-fixture-project",
            )
        self.assertFalse(other_path.exists())

    def test_each_candidate_requires_its_own_passing_review(self) -> None:
        round_id = self.prepare()
        self.store.reserve(round_id, evidence_ready=True)
        self.store.finish(
            round_id,
            status="succeeded",
            outputs=[self.fixture, self.fixture],
            note="离线模拟：单次响应含两张候选，均完整保存。",
        )
        self.store.review(
            round_id,
            candidate="output-01.png",
            report="第一张离线候选通过。",
            verdict="pass",
            blockers=[],
            unknowns=[],
        )
        first_final = self.store.promote(round_id, candidate="output-01.png")
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-02.png")
        self.store.review(
            round_id,
            candidate="output-02.png",
            report="第二张离线候选有独立阻断项。",
            verdict="fail",
            blockers=["第二张候选待修改"],
            unknowns=[],
        )
        self.assertEqual(self.store.promote(round_id, candidate="output-01.png"), first_final)
        with self.assertRaises(StoreError):
            self.store.promote(round_id, candidate="output-02.png")
        self.assertEqual(len(list((self.task_dir / "final_output").glob("*.png"))), 1)
        self.assertEqual(self.store.recover()["remaining"], 3)


if __name__ == "__main__":
    unittest.main()
