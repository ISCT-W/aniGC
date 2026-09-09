import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from anigc.backends import make_backend, resolve_model
from anigc.backends.gpt import GPTBackend
from anigc.backends.types import BackendError, ImageInput, ImageRequest
from anigc.config import gpt_key
from anigc.generation import check_request, generate_round
from anigc.task_store import TaskStore, StoreError
from test_task_store import PNG


class GPTTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock(return_value=(200, json.dumps({'data': [{'b64_json': base64.b64encode(PNG).decode()}]}).encode()))
        self.backend = GPTBackend('fake-key', transport=self.transport)
        self.request = ImageRequest('gpt-image-2.5-flare', 'test', pixel_size='1024x1024', quality='low')

    def test_text_route_and_exact_model(self):
        result = self.backend.generate(self.request)
        url, headers, body, timeout = self.transport.call_args.args
        self.assertTrue(url.endswith('/generations'))
        self.assertEqual(json.loads(body), dict(model=self.request.model, prompt='test', n=1, size='1024x1024', quality='low', output_format='png'))
        self.assertEqual(result.images[0].data, PNG)
        self.transport.assert_called_once()

    def test_reference_generation_and_edit_use_multipart_in_input_order(self):
        for operation, roles in [('generate', ('reference', 'reference')), ('edit', ('base', 'reference'))]:
            request = replace(self.request, operation=operation, inputs=tuple(ImageInput(r, PNG + str(i).encode(), 'image/png') for i, r in enumerate(roles)))
            url, content_type, body = self.backend.build_payload(request)
            self.assertTrue(url.endswith('/edits'))
            self.assertIn('multipart/form-data', content_type)
            self.assertLess(body.index(PNG+b'0'), body.index(PNG+b'1'))
            self.assertEqual(body.count(b'name="image[]"'), 2)

    def test_invalid_settings_rejected_before_transport(self):
        invalid = [dict(model='gemini-3-pro-image'), dict(image_size='1K'), dict(pixel_size='256x256'), dict(pixel_size='1024x1025'), dict(aspect_ratio='3:2'), dict(quality='bogus'), dict(operation='edit'), dict(inputs=(ImageInput('mask', PNG, 'image/png'),)), dict(model='gpt-image-2', quality='max')]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(BackendError):
                self.backend.generate(replace(self.request, **change))
        self.transport.assert_not_called()

    def test_partial_response_never_complete(self):
        result = self.backend._parse({'data': [{'b64_json': base64.b64encode(PNG).decode()}, {'b64_json': 'broken'}]})
        self.assertFalse(result.complete)
        self.assertEqual(len(result.images), 1)

    def test_http_failures_and_disconnect_never_retry(self):
        for status, outcome in [(401, 'failed'), (429, 'failed'), (500, 'unknown'), (408, 'unknown')]:
            self.transport.reset_mock()
            self.transport.return_value = (status, b'secret body')
            with self.assertRaises(BackendError) as caught:
                self.backend.generate(self.request)
            self.assertEqual(caught.exception.status, outcome)
            self.assertNotIn('secret', str(caught.exception))
            self.transport.assert_called_once()
        self.transport.side_effect = TimeoutError('secret')
        with self.assertRaises(BackendError) as caught:
            self.backend.generate(self.request)
        self.assertEqual(caught.exception.status, 'unknown')

    def test_config_isolation_and_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory)/'.env'
            env.write_text('GPT_API_KEY=file-key\nGPT_IMAGE_MODEL=gpt-image-2.5-flare\nGEMINI_IMAGE_MODEL=gemini-3-pro-image\nUNRELATED="broken\n')
            self.assertEqual(gpt_key(env), 'file-key')
            self.assertEqual(resolve_model('gpt', env_file=env), self.request.model)
            self.assertEqual(resolve_model('gpt', 'gpt-image-2', env_file=env), 'gpt-image-2')
            with patch.dict(os.environ, {'GPT_API_KEY': 'process-key'}):
                self.assertEqual(gpt_key(env), 'process-key')
            env.write_text('GEMINI_API_KEY=other-key\n')
            with self.assertRaises(StoreError): gpt_key(env)
            with self.assertRaises(StoreError): resolve_model('gpt', env_file=env)

    def test_frozen_fields_and_live_runner(self):
        with tempfile.TemporaryDirectory() as directory, patch('anigc.config.reference_project', return_value='fixture'):
            store = TaskStore.create(Path(directory)/'task', 'test', 'test', mode='generation', authorization='test fixture', project_id='fixture', limit=1)
            r = store.prepare('record', 'no references', 'gpt', self.request.model, submission='test', pixel_size='1024x1024', quality='low')
            with patch('anigc.backends.gpt_key', side_effect=AssertionError('no key')):
                _, req = check_request(store, r)
            self.assertEqual(req.pixel_size, '1024x1024')
            with patch('anigc.backends.gpt_key', return_value='fake-key'), patch('anigc.backends.gpt._http_transport', self.transport):
                generate_round(store, r, execute=True, evidence_ready=True)
            self.assertEqual(store.recover()['remaining'], 0)
            self.assertTrue((store.path/'rounds/001/output-01.png').exists())
            self.transport.assert_called_once()

    def test_changed_quality_breaks_frozen_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore.create(Path(directory)/'task', 'test', 'test')
            r = store.prepare('record', 'refs', 'gpt', self.request.model, submission='test', quality='low')
            state = json.loads((store.path/'state.json').read_text())
            state['attempts'][0]['request']['quality'] = 'high'
            (store.path/'state.json').write_text(json.dumps(state))
            with self.assertRaises(StoreError): check_request(store, r)

if __name__ == '__main__': unittest.main()
