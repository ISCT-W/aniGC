"""Offline integration tests: fake backends, real local reservations and files."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anigc.backends import make_backend, resolve_model
from anigc.backends.gemini import GeminiBackend
from anigc.backends.types import BackendError, ImageOutput, ImageResult
from anigc.cli import main
from anigc.config import gemini_key, read_settings
from anigc.generation import check_request, generate_round
from anigc.task_store import StoreError, TaskStore
from test_task_store import PNG


class FakeBackend:
    production = False

    def __init__(self, name="fixture-a", error=None, complete=True):
        self.name, self.error, self.complete = name, error, complete
        self.calls = 0
        self.requests = []

    def validate(self, request):
        if not request.prompt:
            raise BackendError("缺少模拟输入", "not_sent")

    def generate(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.error:
            raise self.error
        return ImageResult(images=(ImageOutput(PNG, "image/png"),), model="fixture-v1",
                           text="仅离线测试；不是实际生成。", complete=self.complete)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore.create(self.root / "task", "离线测试，无真实生图授权", "适配器离线测试", limit=4)
        self.image = self.root / "reference.png"
        self.image.write_bytes(PNG)

    def prepare(self, backend="fixture-a", model="fixture"):
        return self.store.prepare("本地记录正文（不应发送给模型）", "R01：固定测试图片，不是实际角色资料",
                                  backend, model, inputs=[("reference", self.image)],
                                  submission="精确提交文字，保留换行。\n第二行。", aspect_ratio="1:1")

    def test_only_exact_submission_and_frozen_images_are_sent(self):
        r = self.prepare()
        self.image.write_bytes(PNG + b"changed source outside task")
        backend = FakeBackend()
        generate_round(self.store, r, execute=True, evidence_ready=True, backend=backend)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(backend.requests[0].prompt, "精确提交文字，保留换行。\n第二行。")
        self.assertEqual(backend.requests[0].inputs[0].data, PNG)
        self.assertEqual(self.store.recover()["remaining"], 3)
        self.assertFalse((self.store.path / "final_output").exists())
        self.assertTrue((self.store.path / "rounds" / r / "response.md").is_file())

    def test_second_backend_uses_same_runner_and_shared_budget(self):
        first, second = FakeBackend("fixture-a"), FakeBackend("fixture-b")
        generate_round(self.store, self.prepare(), execute=True, evidence_ready=True, backend=first)
        generate_round(self.store, self.prepare("fixture-b"), execute=True, evidence_ready=True, backend=second)
        self.assertEqual((first.calls, second.calls), (1, 1))
        self.assertEqual(self.store.recover()["remaining"], 2)

    def test_no_execute_no_evidence_or_offline_production_never_sends(self):
        r = self.prepare()
        backend = FakeBackend()
        with self.assertRaises(StoreError):
            generate_round(self.store, r, backend=backend)
        with self.assertRaises(StoreError):
            generate_round(self.store, r, execute=True, backend=backend)
        backend.production = True
        with self.assertRaises(StoreError):
            generate_round(self.store, r, execute=True, evidence_ready=True, backend=backend)
        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.store.recover()["remaining"], 4)

    def test_fifth_actual_send_is_blocked_after_four_sent_failures(self):
        backend = FakeBackend(error=BackendError("模拟已发送失败", "failed"))
        for _ in range(4):
            with self.assertRaises(BackendError):
                generate_round(self.store, self.prepare(), execute=True, evidence_ready=True, backend=backend)
        with self.assertRaises(StoreError):
            generate_round(self.store, self.prepare(), execute=True, evidence_ready=True, backend=backend)
        self.assertEqual(backend.calls, 4)
        self.assertEqual(self.store.recover()["remaining"], 0)

    def test_timeout_survives_restart_and_does_not_send_again(self):
        backend = FakeBackend(error=BackendError("模拟传输超时", "unknown"))
        r = self.prepare()
        with self.assertRaises(BackendError):
            generate_round(self.store, r, execute=True, evidence_ready=True, backend=backend)
        resumed = TaskStore(self.store.path)
        with self.assertRaises(StoreError):
            generate_round(resumed, r, execute=True, evidence_ready=True, backend=backend)
        with self.assertRaises(StoreError):
            generate_round(resumed, self.prepare(), execute=True, evidence_ready=True, backend=backend)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(resumed.recover()["unresolved"], [r])

    def test_disk_interruption_collects_saved_response_without_resending(self):
        backend = FakeBackend()
        r = self.prepare()
        with patch.object(self.store, "collect", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(StoreError):
                generate_round(self.store, r, execute=True, evidence_ready=True, backend=backend)
        resumed = TaskStore(self.store.path)
        self.assertEqual(resumed.recover()["unresolved"], [r])
        resumed.collect(r)
        resumed.collect(r)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(resumed.recover()["remaining"], 3)
        self.assertEqual(resumed.recover()["unresolved"], [])
        self.assertEqual((resumed.path / f"rounds/{r}/output-01.png").read_bytes(), PNG)

    def test_incomplete_response_keeps_image_but_cannot_be_delivered(self):
        r = self.prepare()
        generate_round(self.store, r, execute=True, evidence_ready=True, backend=FakeBackend(complete=False))
        self.store.review(r, "output-01.png", "离线预设通过，不代表真实监修", "pass")
        with self.assertRaises(StoreError):
            self.store.promote(r, "output-01.png")
        self.assertEqual((self.store.path / f"rounds/{r}/output-01.png").read_bytes(), PNG)

    def test_response_manifest_survives_interruption_before_report_is_saved(self):
        r = self.prepare()
        real_file = self.store._file

        def interrupt_report(relative, data, adopt=False):
            if relative.endswith("/response.md"):
                raise OSError("simulated interruption before response report write")
            return real_file(relative, data, adopt=adopt)

        with patch.object(self.store, "_file", side_effect=interrupt_report):
            with self.assertRaises(StoreError):
                generate_round(self.store, r, execute=True, evidence_ready=True, backend=FakeBackend(complete=False))
        resumed = TaskStore(self.store.path)
        attempt = resumed.snapshot()["attempts"][0]
        self.assertFalse(attempt["receipt"]["complete"])
        self.assertTrue((resumed.path / f"rounds/{r}/output-01.png").is_file())
        with self.assertRaises(StoreError):
            resumed.collect(r)
        with self.assertRaises(StoreError):
            resumed.finish(r, "succeeded", [self.image], note="不能绕过缺失的响应报告与不完整标记")
        self.assertEqual(resumed.recover()["remaining"], 3)

    def test_changed_model_or_settings_cannot_silently_change_request(self):
        r = self.prepare()
        state_path = self.store.path / "state.json"
        state = json.loads(state_path.read_text())
        state["attempts"][0]["model"] = "another-model"
        state_path.write_text(json.dumps(state))
        backend = FakeBackend()
        with self.assertRaises(StoreError):
            generate_round(self.store, r, execute=True, evidence_ready=True, backend=backend)
        self.assertEqual(backend.calls, 0)

    def test_changed_submission_is_rejected_before_sending(self):
        r = self.prepare()
        (self.store.path / f"rounds/{r}/submission.md").write_text("different text")
        backend = FakeBackend()
        with self.assertRaises(StoreError):
            generate_round(self.store, r, execute=True, evidence_ready=True, backend=backend)
        self.assertEqual(backend.calls, 0)

    def test_changed_response_record_blocks_delivery(self):
        r = self.prepare()
        generate_round(self.store, r, execute=True, evidence_ready=True, backend=FakeBackend())
        self.store.review(r, "output-01.png", "离线预设通过；只验证版本约束", "pass")
        (self.store.path / f"rounds/{r}/response.md").write_text("changed provider record")
        with self.assertRaises(StoreError):
            self.store.promote(r, "output-01.png")

    def test_pending_routes_fail_explicitly_and_never_fallback(self):
        for name in ("gpt-image-2", "clip-studio"):
            with self.subTest(name=name), self.assertRaises(StoreError):
                make_backend(name)

    def test_removed_route_cannot_prepare_or_send_but_history_recovers(self):
        # Old records remain readable, but the removed route cannot execute.
        r = self.prepare("codex-image", "codex-managed")
        before = self.store.recover()["remaining"]
        with self.assertRaises(StoreError):
            check_request(self.store, r)
        with self.assertRaises(StoreError):
            generate_round(self.store, r, execute=True, evidence_ready=True)
        self.assertEqual(self.store.recover()["remaining"], before)
        record = self.root / "record.md"
        record.write_text("offline fixture")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for extra in ([], ["--api-text-file", str(record)]):
                code = main(["prepare", str(self.store.path), "--backend", "codex-image",
                             "--model", "codex-managed", "--prompt-file", str(record),
                             "--reference-file", str(record), *extra])
                self.assertEqual(code, 2)
        self.assertEqual(len(self.store.snapshot()["attempts"]), 1)

    def test_validation_has_no_key_loading_or_network(self):
        r = self.prepare("gemini", "gemini-2.5-flash-image")
        with patch("anigc.backends.gemini_key", side_effect=AssertionError("key must not load")), \
             patch("anigc.backends.gemini._http_transport", side_effect=AssertionError("network forbidden")):
            _, request = check_request(self.store, r)
            self.assertEqual(request.model, "gemini-2.5-flash-image")
        self.assertEqual(self.store.recover()["remaining"], 4)

    def test_cli_prepares_checks_and_rejects_execution_without_flag(self):
        prompt, reference, submission = [self.root / name for name in ("prompt.md", "reference.md", "submission.md")]
        prompt.write_text("仅本地记录")
        reference.write_text("R01：离线样本")
        submission.write_text("精确提交文字")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
             patch("anigc.backends.gemini._http_transport", side_effect=AssertionError("network forbidden")):
            code = main(["prepare", str(self.store.path), "--prompt-file", str(prompt),
                         "--reference-file", str(reference), "--api-text-file", str(submission),
                         "--backend", "gemini", "--model", "gemini-2.5-flash-image"])
            self.assertEqual(code, 0)
            self.assertEqual(main(["check-request", str(self.store.path), "001"]), 0)
            self.assertEqual(main(["generate", str(self.store.path), "001"]), 2)
        self.assertEqual(self.store.recover()["remaining"], 4)

    def test_cli_limits_default_to_six_and_preserve_explicit_values(self):
        brief, authorization = self.root / "brief.md", self.root / "authorization.md"
        brief.write_text("仅离线测试 CLI 次数设置。")
        authorization.write_text("明确开启新的离线测试批次，不构成生图授权。")
        with redirect_stdout(io.StringIO()), patch("anigc.cli.generate_round", side_effect=AssertionError("no model calls")):
            for initial_limit, option in ((6, []), (4, ["--limit", "4"])):
                with self.subTest(initial_limit=initial_limit):
                    path = self.root / f"cli-limit-{initial_limit}"
                    self.assertEqual(main(["init", str(path), "--title", "离线测试", "--brief-file", str(brief), *option]), 0)
                    task = TaskStore(path)
                    self.assertEqual(task.snapshot()["batches"][0]["limit"], initial_limit)
                    self.assertEqual(main(["new-batch", str(path), "--authorization-file", str(authorization)]), 0)
                    self.assertEqual(main(["new-batch", str(path), "--authorization-file", str(authorization), "--limit", "4"]), 0)
                    self.assertEqual([batch["limit"] for batch in task.snapshot()["batches"]], [initial_limit, 6, 4])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"

    def test_model_is_independent_and_explicit_choice_wins(self):
        self.path.write_text('GEMINI_MODEL="general-model"\nGEMINI_API_KEY="not-read-for-model"\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_model("gemini", env_file=self.path), "gemini-2.5-flash-image")
            self.path.write_text('GEMINI_IMAGE_MODEL="gemini-3.1-flash-image"\n')
            self.assertEqual(resolve_model("gemini", env_file=self.path), "gemini-3.1-flash-image")
            self.assertEqual(resolve_model("gemini", "gemini-3-pro-image", env_file=self.path), "gemini-3-pro-image")

    def test_keys_are_parsed_without_shell_expansion_and_environment_wins(self):
        self.path.write_text('GEMINI_API_KEY="fake#key" # comment\nUNRELATED="unterminated\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gemini_key(self.path), "fake#key")
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "env-fake-key"}, clear=True):
            self.assertEqual(gemini_key(self.path), "env-fake-key")
        self.path.write_text("GEMINI_API_KEY='$(never-execute)'\n")
        self.assertEqual(read_settings(["GEMINI_API_KEY"], self.path, environ={})["GEMINI_API_KEY"], "$(never-execute)")

    def test_invalid_credentials_do_not_expose_their_value(self):
        secret = "do-not-output-this"
        self.path.write_text('GEMINI_API_KEY="' + secret + '\n')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(StoreError) as error:
                gemini_key(self.path)
        self.assertNotIn(secret, str(error.exception))


if __name__ == "__main__":
    unittest.main()
