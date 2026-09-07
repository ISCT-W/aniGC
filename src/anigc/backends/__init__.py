"""Explicit backend selection. Future routes are visible but never fall back."""

from dataclasses import dataclass
from typing import Callable

from .types import ImageBackend
from ..config import DOTENV, gemini_key, image_model
from ..task_store import StoreError


@dataclass(frozen=True)
class BackendSpec:
    name: str
    route: str
    description: str
    factory: Callable[..., ImageBackend] | None = None


def _gemini(*, live=False, env_file=DOTENV, timeout=120.0):
    from .gemini import GeminiBackend
    return GeminiBackend(api_key=gemini_key(env_file) if live else "", timeout=timeout)


BACKENDS = {
    "gemini": BackendSpec("gemini", "api", "Nano Banana 系列；已实现 REST 适配器，真实调用待验证", _gemini),
    "gpt-image-2": BackendSpec("gpt-image-2", "tool_or_api", "后续选内置工具或 OpenAI API 接入"),
    "clip-studio": BackendSpec("clip-studio", "desktop", "电脑绘画、可编辑 .clip 工程与每轮导出图"),
}


def backend_spec(name):
    if name not in BACKENDS:
        raise StoreError(f"未知制作后端：{name}；使用 backends 查看已声明路线")
    return BACKENDS[name]


def make_backend(name, *, live=False, env_file=DOTENV, timeout=120.0):
    spec = backend_spec(name)
    if spec.factory is None:
        raise StoreError(f"{name} 是预留的 {spec.route} 路线，尚未实现；不会切换到其他后端")
    return spec.factory(live=live, env_file=env_file, timeout=timeout)


def resolve_model(name, explicit=None, *, env_file=DOTENV):
    backend_spec(name)
    if explicit:
        return explicit
    if name == "gemini":
        return image_model(env_file=env_file)
    raise StoreError("此后端需要显式模型或制作工具版本标识")
