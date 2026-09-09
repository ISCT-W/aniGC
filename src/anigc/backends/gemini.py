"""Minimal Gemini image backend; no retry, credential loading, or file writes.

REST schema and model capabilities checked against official docs on 2026-09-07:
https://ai.google.dev/api/generate-content#ImageConfig
https://ai.google.dev/gemini-api/docs/generate-content/image-generation
https://ai.google.dev/gemini-api/docs/generate-content/image-understanding

Only independently supplied image/text turns are implemented. We do not request
or persist thought parts, signatures, grounding results, or a chat transcript.
The caller must reserve its request budget before calling ``generate``.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from typing import Callable

from .types import BackendError, ImageInput, ImageOutput, ImageRequest, ImageResult


Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/"
MAX_INLINE_BYTES = 20_000_000
COMMON_RATIOS = frozenset({"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"})
FLASH_RATIOS = COMMON_RATIOS | {"1:4", "4:1", "1:8", "8:1"}
# The three-input limit for 2.5 is this adapter's conservative choice based on
# Google's "works best" guidance, not a claim about the server's hard limit.
MODEL_CAPABILITIES = {
    "gemini-2.5-flash-image": (3, COMMON_RATIOS, frozenset()),
    "gemini-3-pro-image": (14, COMMON_RATIOS, frozenset({"1K", "2K", "4K"})),
    "gemini-3.1-flash-image": (14, FLASH_RATIOS, frozenset({"512", "1K", "2K", "4K"})),
}
MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
USAGE_FIELDS = frozenset({
    "promptTokenCount", "candidatesTokenCount", "totalTokenCount",
    "cachedContentTokenCount", "thoughtsTokenCount", "toolUsePromptTokenCount",
})


from .common import _http_transport, _is_raster


def _encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class GeminiBackend:
    name = "gemini"
    production = True

    def __init__(self, api_key: str, transport: Transport | None = None, timeout: float = 120.0):
        if (
            not isinstance(api_key, str)
            or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
        ):
            raise BackendError("Gemini API 凭据格式不适合请求头。", "not_sent")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise BackendError("Gemini 请求超时设置必须为有限正数。", "not_sent")
        if transport is not None and not callable(transport):
            raise BackendError("Gemini 传输实现不可调用。", "not_sent")
        self._api_key = api_key
        self._transport = transport if transport is not None else _http_transport
        self._timeout = float(timeout)

    def validate(self, request: ImageRequest) -> None:
        if not isinstance(request, ImageRequest):
            raise BackendError("Gemini 输入必须是图片请求。", "not_sent")
        if not isinstance(request.model, str) or request.model not in MODEL_CAPABILITIES:
            raise BackendError("Gemini 模型标识尚未接入；请使用已核实的正式图片模型标识。", "not_sent")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise BackendError("完整生成指令不能为空。", "not_sent")
        if not isinstance(request.operation, str) or request.operation not in {"generate", "edit"}:
            raise BackendError("Gemini 仅接入生成与图片加文字编辑，未接入遮罩编辑。", "not_sent")
        if not isinstance(request.inputs, tuple) or not all(isinstance(item, ImageInput) for item in request.inputs):
            raise BackendError("图片输入必须是按顺序保存的图片输入元组。", "not_sent")
        if request.pixel_size is not None or request.quality is not None:
            raise BackendError("Gemini 尚未接入 pixel_size/quality 参数。", "not_sent")
        max_inputs, ratios, sizes = MODEL_CAPABILITIES[request.model]
        if len(request.inputs) > max_inputs:
            message = "当前适配器对 Gemini 2.5 限制为最多 3 张输入图。" if max_inputs == 3 else "该模型最多接入 14 张输入图。"
            raise BackendError(message, "not_sent")
        if request.aspect_ratio is not None and (not isinstance(request.aspect_ratio, str) or request.aspect_ratio not in ratios):
            raise BackendError("所选模型不支持该图片比例。", "not_sent")
        if request.image_size is not None and (not isinstance(request.image_size, str) or request.image_size not in sizes):
            raise BackendError("所选模型不支持该显式图片尺寸；Gemini 2.5 应省略尺寸设置。", "not_sent")
        base_count = 0
        for item in request.inputs:
            if not isinstance(item.role, str) or item.role not in {"reference", "base"}:
                raise BackendError("Gemini 仅接入参考图和编辑底图；不会忽略遮罩或未知输入用途。", "not_sent")
            base_count += item.role == "base"
            if not isinstance(item.mime_type, str) or item.mime_type not in MIME_TYPES or not _is_raster(item.data, item.mime_type):
                raise BackendError("当前 Gemini 适配器只接收文件头匹配的 PNG、JPEG、WebP 图片。", "not_sent")
        if request.operation == "edit" and base_count != 1:
            raise BackendError("图片编辑需要且只接受一张明确的编辑底图。", "not_sent")
        if request.operation == "generate" and base_count:
            raise BackendError("带编辑底图时必须明确使用 edit 操作。", "not_sent")
        try:
            size = len(_encode(self._payload(request)))
        except (TypeError, ValueError, UnicodeError):
            raise BackendError("Gemini 请求无法编码为有效的 JSON。", "not_sent") from None
        if size >= MAX_INLINE_BYTES:
            raise BackendError("内联图片与文字组成的完整请求必须小于 20 MB。", "not_sent")

    @staticmethod
    def _payload(request: ImageRequest) -> dict:
        parts = [{"text": request.prompt}]
        parts.extend({"inlineData": {"mimeType": item.mime_type, "data": base64.b64encode(item.data).decode("ascii")}} for item in request.inputs)
        config = {"responseModalities": ["TEXT", "IMAGE"], "candidateCount": 1}
        image_config = {}
        if request.aspect_ratio is not None:
            image_config["aspectRatio"] = request.aspect_ratio
        if request.image_size is not None:
            image_config["imageSize"] = request.image_size
        if image_config:
            config["imageConfig"] = image_config
        return {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}

    def build_payload(self, request: ImageRequest) -> dict:
        """Return in-memory API input; callers must not persist the base64 body."""
        self.validate(request)
        return self._payload(request)

    def generate(self, request: ImageRequest) -> ImageResult:
        if not self._api_key:
            raise BackendError("实际调用前需要已配置的 Gemini API 凭据。", "not_sent")
        body = _encode(self.build_payload(request))
        url = ENDPOINT + request.model + ":generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        try:
            status, response = self._transport(url, headers, body, self._timeout)
        except Exception:
            # Once transport has been entered, do not guess whether it sent.
            raise BackendError("Gemini 请求传输中断，远端结果未知；未自动重试。", "unknown") from None
        if type(status) is not int or not isinstance(response, bytes):
            raise BackendError("Gemini 传输返回格式异常，结果未知。", "unknown")
        if not 200 <= status < 300:
            outcome = "unknown" if status >= 500 or status == 408 else "failed"
            raise BackendError(f"Gemini 返回 HTTP {status}；未读取错误详情或自动重试。", outcome)
        try:
            document = json.loads(response)
        except (ValueError, UnicodeError):
            raise BackendError("Gemini 响应无法解析，结果未知；未自动重试。", "unknown") from None
        return self._parse(document)

    def _identifier(self, value: object) -> str:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", value) and (not self._api_key or self._api_key not in value):
            return value
        return ""

    def _parse(self, document: object) -> ImageResult:
        if not isinstance(document, dict):
            raise BackendError("Gemini 响应结构异常，结果未知。", "unknown")
        candidates = document.get("candidates", [])
        if not isinstance(candidates, list):
            raise BackendError("Gemini 候选结构异常，结果未知。", "unknown")
        images, texts, reasons, warnings = [], [], [], []
        malformed = False
        for candidate in candidates:
            if not isinstance(candidate, dict):
                malformed = True
                continue
            reason = self._identifier(candidate.get("finishReason"))
            if reason:
                reasons.append(reason)
            content = candidate.get("content", {})
            parts = content.get("parts", []) if isinstance(content, dict) else None
            if not isinstance(parts, list):
                malformed = True
                continue
            for part in parts:
                if not isinstance(part, dict):
                    malformed = True
                    continue
                if part.get("thought"):
                    continue
                if isinstance(part.get("text"), str):
                    texts.append(part["text"].replace(self._api_key, "[凭据已隐藏]") if self._api_key else part["text"])
                if "inlineData" in part:
                    inline = part["inlineData"]
                    if not isinstance(inline, dict) or not isinstance(inline.get("mimeType"), str) or inline["mimeType"] not in MIME_TYPES or not isinstance(inline.get("data"), str):
                        malformed = True
                        continue
                    try:
                        data = base64.b64decode(inline["data"], validate=True)
                    except (ValueError, binascii.Error):
                        malformed = True
                        continue
                    if not _is_raster(data, inline["mimeType"]):
                        malformed = True
                        continue
                    images.append(ImageOutput(data=data, mime_type=inline["mimeType"]))
                elif "fileData" in part:
                    # This inline-only implementation does not follow returned URLs.
                    malformed = True
        if not images:
            if malformed:
                raise BackendError("Gemini 返回的图片数据无法完整解析，结果未知。", "unknown")
            raise BackendError("Gemini 未返回可保存的最终图片，可能仅有文字或被阻断。", "failed")
        if malformed:
            warnings.append("响应有无法解析的内容或图片；已保留全部可读取图片，仍需核对缺失产物。")
        if len(images) > 1:
            warnings.append("请求设置为一个候选，但响应包含多张图片，已全部保留。")
        if any(reason != "STOP" for reason in reasons):
            warnings.append("至少一个候选未以 STOP 正常结束；已保留返回图片，需核对完整性。")
        usage = document.get("usageMetadata", {})
        usage = {key: value for key, value in usage.items() if key in USAGE_FIELDS and type(value) is int and value >= 0} if isinstance(usage, dict) else {}
        return ImageResult(
            images=tuple(images), text="\n".join(texts),
            model=self._identifier(document.get("modelVersion")),
            request_id=self._identifier(document.get("responseId")),
            finish_reasons=tuple(reasons), usage=usage, warnings=tuple(warnings),
            complete=not malformed,
        )
