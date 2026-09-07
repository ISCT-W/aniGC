"""Gemini adapter tests use injected transports or mocked urllib, never a service."""

import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anigc.backends import gemini  # noqa: E402
from anigc.backends.gemini import GeminiBackend  # noqa: E402
from anigc.backends.types import BackendError, ImageInput, ImageRequest  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA1sAAAAASUVORK5CYII="
)
JPEG = b"\xff\xd8\xff\xe0offline-fixture"
KEY = "offline-test-key-should-not-appear"


def image_part(data=PNG, mime="image/png", **extra):
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}, **extra}


def response(parts=None, **extra):
    return {
        "candidates": [{"content": {"parts": [image_part()] if parts is None else parts}, "finishReason": "STOP"}],
        **extra,
    }


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.transport = mock.Mock(return_value=(200, json.dumps(response()).encode()))
        self.backend = GeminiBackend(KEY, transport=self.transport)
        self.request = ImageRequest(model="gemini-3.1-flash-image", prompt="离线测试完整指令。")

    def error(self, request=None, expected="not_sent"):
        with self.assertRaises(BackendError) as caught:
            self.backend.generate(request or self.request)
        self.assertEqual(caught.exception.status, expected)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, repr(caught.exception))
        return caught.exception

    def test_payload_preserves_prompt_image_order_and_explicit_options(self):
        request = replace(
            self.request,
            inputs=(ImageInput("reference", PNG, "image/png"), ImageInput("base", JPEG, "image/jpeg")),
            operation="edit", aspect_ratio="16:9", image_size="2K",
        )
        payload = self.backend.build_payload(request)
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": request.prompt})
        self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), PNG)
        self.assertEqual(base64.b64decode(parts[2]["inlineData"]["data"]), JPEG)
        self.assertEqual(parts[2]["inlineData"]["mimeType"], "image/jpeg")
        self.assertEqual(payload["generationConfig"], {
            "candidateCount": 1, "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "16:9", "imageSize": "2K"},
        })
        self.assertEqual(set(payload), {"contents", "generationConfig"})
        self.transport.assert_not_called()

    def test_original_model_omits_size_and_uses_its_own_capabilities(self):
        request = replace(self.request, model="gemini-2.5-flash-image", aspect_ratio="1:1")
        self.assertEqual(self.backend.build_payload(request)["generationConfig"]["imageConfig"], {"aspectRatio": "1:1"})
        for bad in (replace(request, image_size="1K"), replace(request, aspect_ratio="1:8")):
            with self.subTest(request=bad):
                self.error(bad)
        self.transport.assert_not_called()

    def test_capability_limits_reject_unsupported_requests_before_transport(self):
        reference = ImageInput("reference", PNG, "image/png")
        invalid = [
            replace(self.request, model="nano-banana"),
            replace(self.request, model="gemini-3-pro-image-preview"),
            replace(self.request, model="../outside?key=anything"),
            replace(self.request, prompt=" "),
            replace(self.request, image_size="0.5K"),
            replace(self.request, image_size="1k"),
            replace(self.request, aspect_ratio="7:5"),
            replace(self.request, model="gemini-3-pro-image", image_size="512"),
            replace(self.request, model="gemini-3-pro-image", aspect_ratio="1:8"),
            replace(self.request, inputs=(reference,) * 15),
            replace(self.request, model="gemini-2.5-flash-image", inputs=(reference,) * 4),
            replace(self.request, inputs=(ImageInput("mask", PNG, "image/png"),)),
            replace(self.request, operation="inpaint"),
            replace(self.request, operation="edit"),
            replace(self.request, inputs=(ImageInput("base", PNG, "image/png"),)),
            replace(self.request, operation="edit", inputs=(ImageInput("base", PNG, "image/png"),) * 2),
            replace(self.request, inputs=(ImageInput("reference", PNG, "image/gif"),)),
            replace(self.request, inputs=(ImageInput("reference", PNG, "image/jpeg"),)),
        ]
        for request in invalid:
            with self.subTest(model=request.model, operation=request.operation, size=request.image_size, ratio=request.aspect_ratio):
                self.error(request)
        self.transport.assert_not_called()

    def test_current_models_accept_documented_supported_options(self):
        cases = [
            replace(self.request, model="gemini-2.5-flash-image", inputs=(ImageInput("reference", PNG, "image/png"),) * 3),
            replace(self.request, model="gemini-3-pro-image", image_size="4K", aspect_ratio="21:9"),
            replace(self.request, image_size="512", aspect_ratio="1:8", inputs=(ImageInput("reference", PNG, "image/png"),) * 14),
        ]
        for request in cases:
            with self.subTest(model=request.model):
                self.backend.validate(request)
        self.transport.assert_not_called()

    def test_inline_limit_counts_serialized_base64_and_text(self):
        payload_size = len(json.dumps(self.backend.build_payload(self.request), ensure_ascii=False, separators=(",", ":")).encode())
        with mock.patch.object(gemini, "MAX_INLINE_BYTES", payload_size):
            self.error()
        with mock.patch.object(gemini, "MAX_INLINE_BYTES", payload_size + 1):
            self.backend.validate(self.request)
        # Decoded bytes fit this limit, but the serialized request does not.
        with mock.patch.object(gemini, "MAX_INLINE_BYTES", len(PNG) + 1):
            self.error(replace(self.request, inputs=(ImageInput("reference", PNG, "image/png"),)))
        self.transport.assert_not_called()

    def test_one_call_uses_fixed_endpoint_and_header_only_key(self):
        result = self.backend.generate(self.request)
        self.assertEqual(result.images[0].data, PNG)
        self.assertEqual(result.model, "")
        self.transport.assert_called_once()
        url, headers, body, timeout = self.transport.call_args.args
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image:generateContent")
        self.assertEqual(headers["x-goog-api-key"], KEY)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn(KEY, url)
        self.assertNotIn(KEY.encode(), body)
        self.assertNotIn(KEY, repr(self.backend))
        self.assertEqual(timeout, 120.0)

    def test_all_final_images_survive_while_thoughts_and_signatures_do_not(self):
        document = response([
            {"text": "private thought", "thought": True},
            image_part(JPEG, "image/jpeg", thought=True),
            {"text": "可见结果"},
            image_part(thoughtSignature="private signature"),
            image_part(JPEG, "image/jpeg"),
        ], responseId="response-123", modelVersion="gemini-3.1-flash-image-001", usageMetadata={
            "promptTokenCount": 10, "totalTokenCount": 20, "thoughtsTokenCount": 5,
            "rawSecret": KEY, "candidatesTokenCount": True, "cachedContentTokenCount": -1,
        })
        document["candidates"].append({"content": {"parts": [image_part()]}, "finishReason": "MAX_TOKENS"})
        self.transport.return_value = (200, json.dumps(document).encode())
        result = self.backend.generate(self.request)
        self.assertEqual([(item.data, item.mime_type) for item in result.images], [(PNG, "image/png"), (JPEG, "image/jpeg"), (PNG, "image/png")])
        self.assertEqual(result.text, "可见结果")
        self.assertEqual(result.request_id, "response-123")
        self.assertEqual(result.model, "gemini-3.1-flash-image-001")
        self.assertEqual(result.finish_reasons, ("STOP", "MAX_TOKENS"))
        self.assertEqual(result.usage, {"promptTokenCount": 10, "totalTokenCount": 20, "thoughtsTokenCount": 5})
        self.assertNotIn("private", repr(result))
        self.assertEqual(len(result.warnings), 2)
        self.assertTrue(result.complete)

    def test_http_errors_are_sanitized_and_never_retried(self):
        for status, expected in ((400, "failed"), (401, "failed"), (429, "failed"), (302, "failed"), (408, "unknown"), (500, "unknown"), (503, "unknown")):
            with self.subTest(status=status):
                self.transport.reset_mock()
                self.transport.return_value = (status, f"raw error includes {KEY}".encode())
                self.error(expected=expected)
                self.transport.assert_called_once()

    def test_transport_exceptions_are_unknown_without_leaking_cause(self):
        for failure in (TimeoutError(KEY), ConnectionResetError(KEY), URLError(KEY)):
            with self.subTest(kind=type(failure).__name__):
                self.transport.reset_mock()
                self.transport.side_effect = failure
                error = self.error(expected="unknown")
                self.assertTrue(error.__suppress_context__)
                self.transport.assert_called_once()

    def test_malformed_response_remains_unknown(self):
        for data in (b"not-json", b"\xff", b"[]", b'{"candidates":{}}', b'{"candidates":[{"content":{"parts":[{"inlineData":{"mimeType":"image/png","data":"not base64"}}]}}]}'):
            with self.subTest(data=data[:20]):
                self.transport.return_value = (200, data)
                self.error(expected="unknown")

    def test_text_only_blocked_or_thought_only_is_not_success(self):
        for document in (
            response([{"text": "我已经完成了图片。"}]),
            {"promptFeedback": {"blockReason": "SAFETY"}},
            response([image_part(thought=True), {"text": "hidden thought", "thought": True}]),
        ):
            with self.subTest(document_type=tuple(document)):
                self.transport.return_value = (200, json.dumps(document).encode())
                self.error(expected="failed")

    def test_partial_malformed_response_preserves_readable_images_and_warns(self):
        document = response([
            image_part(),
            {"inlineData": {"mimeType": "image/png", "data": "bad data"}},
            {"fileData": {"fileUri": "https://example.invalid/signed?secret=private"}},
            image_part(JPEG, "image/jpeg"),
        ])
        self.transport.return_value = (200, json.dumps(document).encode())
        result = self.backend.generate(self.request)
        self.assertEqual([image.data for image in result.images], [PNG, JPEG])
        self.assertTrue(any("缺失产物" in warning for warning in result.warnings))
        self.assertNotIn("signed", repr(result))
        self.assertFalse(result.complete)

    def test_returned_metadata_does_not_expose_key_or_remote_urls(self):
        document = response([
            image_part(), {"text": f"returned text {KEY}"},
        ], responseId=f"https://example.invalid/?key={KEY}", modelVersion=KEY)
        self.transport.return_value = (200, json.dumps(document).encode())
        result = self.backend.generate(self.request)
        self.assertEqual(result.request_id, "")
        self.assertEqual(result.model, "")
        self.assertNotIn(KEY, repr(result))
        self.assertIn("凭据已隐藏", result.text)

    def test_default_transport_installs_redirect_blocker_and_does_not_read_error(self):
        error_body = mock.Mock(wraps=io.BytesIO(f"private {KEY}".encode()))
        http_error = HTTPError("https://generativelanguage.googleapis.com/", 302, "redirect", {"Location": "https://example.invalid"}, error_body)
        opener = mock.Mock()
        opener.open.side_effect = http_error
        with mock.patch.object(gemini, "build_opener", return_value=opener) as build:
            backend = GeminiBackend(KEY)
            with self.assertRaises(BackendError) as caught:
                backend.generate(self.request)
        self.assertEqual(caught.exception.status, "failed")
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, gemini._NoRedirect)
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://example.invalid"))
        error_body.read.assert_not_called()
        opener.open.assert_called_once()

    def test_bad_key_or_timeout_stops_locally(self):
        for key, timeout in ((" ", 1), ("header\r\ninjection", 1), (KEY, 0), (KEY, float("inf")), (KEY, True)):
            with self.subTest(timeout=timeout):
                with self.assertRaises(BackendError) as caught:
                    GeminiBackend(key, transport=self.transport, timeout=timeout)
                self.assertEqual(caught.exception.status, "not_sent")
                if key.strip():
                    self.assertNotIn(key, str(caught.exception))
        self.transport.assert_not_called()

    def test_no_key_allows_offline_validation_but_cannot_send(self):
        backend = GeminiBackend("", transport=self.transport)
        backend.validate(self.request)
        self.assertEqual(backend.build_payload(self.request)["contents"][0]["parts"][0]["text"], self.request.prompt)
        with self.assertRaises(BackendError) as caught:
            backend.generate(self.request)
        self.assertEqual(caught.exception.status, "not_sent")
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
