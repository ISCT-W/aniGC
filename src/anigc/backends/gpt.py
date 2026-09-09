"""OpenAI Images API adapter. Single send, no fallback or hidden retries.

Capabilities: https://developers.openai.com/api/docs/guides/image-generation
Only explicitly listed models and PNG output are supported by this adapter.
"""
import base64
import binascii
import json
import math
import re
import secrets

from .common import _http_transport, _is_raster
from .types import BackendError, ImageInput, ImageOutput, ImageRequest, ImageResult

ENDPOINT = "https://api.openai.com/v1/images/"
MODELS = frozenset({"gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"})


class GPTBackend:
    name = "gpt"
    production = True

    def __init__(self, api_key, transport=None, timeout=120.0):
        if not isinstance(api_key, str) or any(ord(c) < 33 or ord(c) > 126 for c in api_key):
            raise BackendError("GPT 凭据格式无效。", "not_sent")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise BackendError("GPT 超时须为有限正数。", "not_sent")
        if transport is not None and not callable(transport):
            raise BackendError("GPT 传输实现不可调用。", "not_sent")
        self._api_key, self._timeout = api_key, float(timeout)
        self._transport = transport or _http_transport

    def validate(self, request):
        def reject(message):
            raise BackendError(message, "not_sent")
        if not isinstance(request, ImageRequest):
            reject("GPT 需要图片请求。")
        if not isinstance(request.model, str) or request.model not in MODELS:
            reject("GPT 模型尚未接入；不会替换所选型号。")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            reject("完整生成指令不能为空。")
        if request.operation not in ("generate", "edit"):
            reject("GPT 仅接入 generate/edit。")
        if request.image_size is not None:
            reject("GPT 不接受 Gemini 尺寸档位；请使用 --pixel-size。")
        qualities = ("auto", "low", "medium", "high") + (() if request.model == "gpt-image-2" else ("xhigh", "max"))
        if request.quality is not None and request.quality not in qualities:
            reject("所选 GPT 模型不支持该质量设置。")
        size = request.pixel_size or "auto"
        if size != "auto":
            match = re.fullmatch(r"([1-9][0-9]{0,3})x([1-9][0-9]{0,3})", size) if isinstance(size, str) else None
            if not match:
                reject("像素尺寸须为 WIDTHxHEIGHT 或 auto。")
            w, h = map(int, match.groups())
            if w % 16 or h % 16 or max(w, h) > 3840 or max(w, h) > 3 * min(w, h) or not 655360 <= w*h <= 8294400:
                reject("GPT 尺寸须为 16 的倍数、边长不超过 3840、比例不超过 3:1、像素总数在 655360–8294400。")
        if request.aspect_ratio is not None:
            ratio = re.fullmatch(r"([1-9][0-9]?):([1-9][0-9]?)", request.aspect_ratio) if isinstance(request.aspect_ratio, str) else None
            if size == "auto" or not ratio:
                reject("GPT 指定比例时须同时提供匹配的 --pixel-size。")
            a, b = map(int, ratio.groups())
            if w*b != h*a:
                reject("GPT 像素尺寸与比例不一致。")
        if not isinstance(request.inputs, tuple) or not all(isinstance(i, ImageInput) for i in request.inputs):
            reject("图片输入必须为有序图片元组。")
        # Conservative local input cap, not a claim about the API maximum.
        if len(request.inputs) > 14 or sum(len(i.data) for i in request.inputs) > 20_000_000:
            reject("当前 GPT 适配器限制 14 张输入图、合计不超过 20 MB。")
        for item in request.inputs:
            if item.role not in ("reference", "base"):
                reject("GPT 当前未接入遮罩或未知图片用途。")
            if not _is_raster(item.data, item.mime_type):
                reject("GPT 输入须为文件头匹配的 PNG/JPEG/WebP。")
        bases = sum(i.role == "base" for i in request.inputs)
        if (request.operation == "edit" and bases != 1) or (request.operation == "generate" and bases):
            reject("edit 需要一张底图；generate 不接受底图。")
        try:
            request.prompt.encode("utf-8")
        except UnicodeError:
            reject("GPT 文字无法编码。")

    def build_payload(self, request):
        self.validate(request)
        fields = dict(model=request.model, prompt=request.prompt, n=1, size=request.pixel_size or "auto",
                      quality=request.quality or "auto", output_format="png")
        if not request.inputs:
            return ENDPOINT + "generations", "application/json", json.dumps(fields, ensure_ascii=False).encode()
        boundary = "anigc-" + secrets.token_hex(24)
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        for index, item in enumerate(request.inputs):
            suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[item.mime_type]
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="input-{index}.{suffix}"\r\nContent-Type: {item.mime_type}\r\n\r\n'.encode() + item.data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return ENDPOINT + "edits", "multipart/form-data; boundary=" + boundary, b"".join(parts)

    def generate(self, request):
        url, content_type, body = self.build_payload(request)
        if not self._api_key:
            raise BackendError("实际调用需要 GPT_API_KEY。", "not_sent")
        try:
            status, raw = self._transport(url, {"Authorization": "Bearer " + self._api_key, "Content-Type": content_type}, body, self._timeout)
        except Exception:
            raise BackendError("GPT 传输中断，结果未知；未重试。", "unknown") from None
        if type(status) is not int or not isinstance(raw, bytes):
            raise BackendError("GPT 传输响应异常。", "unknown")
        if not 200 <= status < 300:
            raise BackendError(f"GPT 返回 HTTP {status}；未记录错误正文或重试。", "unknown" if status >= 500 or status == 408 else "failed")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BackendError("GPT 响应无法解析，结果未知。", "unknown") from None
        return self._parse(document)

    def _parse(self, document):
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            raise BackendError("GPT 响应结构异常。", "unknown")
        images, texts, malformed = [], [], False
        for item in document["data"]:
            try:
                data = base64.b64decode(item["b64_json"], validate=True)
                if not _is_raster(data, "image/png"):
                    raise ValueError()
            except (TypeError, KeyError, ValueError, binascii.Error):
                malformed = True
                continue
            images.append(ImageOutput(data, "image/png"))
            if isinstance(item.get("revised_prompt"), str):
                value = item["revised_prompt"]
                texts.append(value.replace(self._api_key, "[凭据已隐藏]") if self._api_key else value)
        if not images:
            raise BackendError("GPT 未返回可保存图片。", "unknown" if malformed else "failed")
        usage = {}
        source = document.get("usage", {})
        if isinstance(source, dict):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                if type(source.get(key)) is int and source[key] >= 0:
                    usage[key] = source[key]
            details = source.get("input_tokens_details", {})
            if isinstance(details, dict):
                for key in ("text_tokens", "image_tokens"):
                    if type(details.get(key)) is int and details[key] >= 0:
                        usage["input_" + key] = details[key]
        warnings = ["响应部分图片无法解析；已保存可读图片，本轮不可交付。"] if malformed else []
        if len(images) > 1:
            warnings.append("请求一张但返回多张，已全部保存。")
        model = document.get("model", "")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model) or (self._api_key and self._api_key in model):
            model = ""
        return ImageResult(tuple(images), text="\n".join(texts), model=model, usage=usage,
                           warnings=tuple(warnings), complete=not malformed)
