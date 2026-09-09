"""Local validation for a Codex session tool; never invokes an API.

The agent calls image_gen after reservation and registers returned files with
finish. Tool availability, actual invocation and visual review remain agent duties.
"""
from .common import _is_raster
from .types import BackendError, ImageInput, ImageRequest

MODEL = "codex-managed"


class CodexImageBackend:
    name = "codex-image"
    production = True

    def validate(self, request):
        def reject(message):
            raise BackendError(message, "not_sent")
        if not isinstance(request, ImageRequest):
            reject("codex-image 需要完整图片请求。")
        if request.model != MODEL:
            reject("内置工具未开放型号选择；使用 codex-managed 标记，实际型号未知。")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            reject("内置工具需要完整实际提交文字。")
        if any(value is not None for value in (request.aspect_ratio, request.image_size, request.pixel_size, request.quality)):
            reject("内置工具未开放这些结构化参数；请在提交文字中描述尺寸、比例与质量要求，结果需验收。")
        if request.operation not in ("generate", "edit"):
            reject("内置路线仅接入 generate/edit。")
        if not isinstance(request.inputs, tuple) or not all(isinstance(i, ImageInput) for i in request.inputs):
            reject("输入图必须按顺序登记。")
        for item in request.inputs:
            if item.role not in ("reference", "base") or not _is_raster(item.data, item.mime_type):
                reject("内置路线只接入 PNG/JPEG/WebP 参考图与底图；未接入独立遮罩参数。")
        bases = sum(i.role == "base" for i in request.inputs)
        if (request.operation == "edit" and bases != 1) or (request.operation == "generate" and bases):
            reject("edit 必须有一张底图；generate 仅接受参考图。")

    def generate(self, request):
        raise BackendError("codex-image 由 Codex 会话调用 image_gen；先 check-request、reserve，再调用工具并 finish。Python 不会代发或切换 API。", "not_sent")
