"""No image tool or network calls: test session handoff and shared accounting."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anigc.backends import make_backend, resolve_model
from anigc.backends.types import BackendError, ImageInput, ImageRequest
from anigc.cli import main
from anigc.generation import check_request, generate_round
from anigc.task_store import StoreError, TaskStore
from test_task_store import PNG


class CodexImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore.create(self.root/'task', 'offline fixture', 'tool flow', limit=2)
        self.prompt = self.root/'prompt.md'
        self.prompt.write_text('Draw a circle.\nNo text.')
        self.image = self.root/'image.png'
        self.image.write_bytes(PNG)

    def cli(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(list(args))

    def prepare(self):
        self.assertEqual(self.cli('prepare', str(self.store.path), '--backend', 'codex-image', '--prompt-file', str(self.prompt), '--reference-file', str(self.prompt), '--submission-file', str(self.prompt)), 0)
        return self.store.snapshot()['attempts'][-1]['id']

    def test_prepare_check_reserve_finish_share_budget(self):
        with patch('anigc.config.read_settings', side_effect=AssertionError('no settings')):
            r = self.prepare()
            saved, request = check_request(self.store, r)
            self.assertEqual(request.model, 'codex-managed')
            self.assertEqual(request.prompt, self.prompt.read_text())
            self.assertEqual(self.cli('reserve', str(self.store.path), r, '--evidence-ready'), 0)
        self.store.finish(r, 'succeeded', [self.image], note='Simulated tool output; not a real generation')
        self.assertEqual(self.store.recover()['remaining'], 1)
        self.assertFalse((self.store.path/'final_output').exists())
        other = self.store.prepare('record', 'refs', 'gemini', 'fixture')
        self.store.reserve(other, evidence_ready=True)
        self.store.finish(other, 'failed', note='simulated failure')
        self.assertEqual(self.store.recover()['remaining'], 0)

    def test_api_execution_rejected_before_reservation(self):
        r = self.prepare()
        with patch('anigc.config.read_settings', side_effect=AssertionError('no settings')):
            with self.assertRaises(StoreError):
                make_backend('codex-image', live=True)
            with self.assertRaises(StoreError):
                generate_round(self.store, r, execute=True, evidence_ready=True)
        self.assertEqual(self.store.recover()['remaining'], 2)
        self.assertEqual(self.store.snapshot()['attempts'][0]['status'], 'prepared')

    def test_no_model_or_parameter_pretence(self):
        request = ImageRequest('codex-managed', 'draw')
        backend = make_backend('codex-image')
        backend.validate(request)
        with self.assertRaises(StoreError): resolve_model('codex-image', 'gpt-image-2')
        for change in [dict(quality='high'), dict(pixel_size='1024x1024'), dict(aspect_ratio='1:1'), dict(image_size='1K'), dict(operation='edit'), dict(inputs=(ImageInput('mask', PNG, 'image/png'),))]:
            with self.subTest(change=change), self.assertRaises(BackendError):
                backend.validate(replace(request, **change))
        backend.validate(replace(request, operation='edit', inputs=(ImageInput('base', PNG, 'image/png'),)))

    def test_input_mutation_and_duplicate_reservation_blocked(self):
        r = self.prepare()
        submission = self.store.path/f'rounds/{r}/submission.md'
        original = submission.read_text()
        submission.write_text('changed')
        self.assertEqual(self.cli('reserve', str(self.store.path), r, '--evidence-ready'), 2)
        self.assertEqual(self.store.recover()['remaining'], 2)
        submission.write_text(original)
        self.assertEqual(self.cli('reserve', str(self.store.path), r, '--evidence-ready'), 0)
        self.assertEqual(self.cli('reserve', str(self.store.path), r, '--evidence-ready'), 2)
        self.store.finish(r, 'unknown', note='simulated lost tool result')
        next_round = self.prepare()
        self.assertEqual(self.cli('reserve', str(self.store.path), next_round, '--evidence-ready'), 2)

    def test_submission_required(self):
        self.assertEqual(self.cli('prepare', str(self.store.path), '--backend', 'codex-image', '--model', 'codex-managed', '--prompt-file', str(self.prompt), '--reference-file', str(self.prompt)), 2)
        self.assertEqual(self.store.snapshot()['attempts'], [])

if __name__ == '__main__': unittest.main()
