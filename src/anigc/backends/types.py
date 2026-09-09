"""Provider-neutral input/output contract for image generation and editing."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ImageInput:
    role: str
    data: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class ImageRequest:
    model: str
    prompt: str
    inputs: tuple[ImageInput, ...] = ()
    aspect_ratio: str | None = None
    image_size: str | None = None
    operation: str = "generate"
    pixel_size: str | None = None
    quality: str | None = None


@dataclass(frozen=True)
class ImageOutput:
    data: bytes = field(repr=False)
    mime_type: str


@dataclass(frozen=True)
class ImageResult:
    images: tuple[ImageOutput, ...]
    text: str = ""
    model: str = ""
    request_id: str = ""
    finish_reasons: tuple[str, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    complete: bool = True


class BackendError(Exception):
    """Only a sanitized message crosses the provider boundary.

    failed: a definitive remote rejection/result; unknown: a response may have
    been lost; not_sent: a positively identified local failure before sending.
    """

    def __init__(self, public_message: str, status: str = "unknown"):
        if status not in {"failed", "unknown", "not_sent"}:
            raise ValueError("Invalid backend error status")
        self.public_message = public_message
        self.status = status
        super().__init__(public_message)


class ImageBackend(Protocol):
    name: str
    production: bool

    def validate(self, request: ImageRequest) -> None: ...

    def generate(self, request: ImageRequest) -> ImageResult: ...
