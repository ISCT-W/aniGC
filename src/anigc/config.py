"""Read only selected settings; never execute or export dotenv contents."""

import os
from pathlib import Path
import re
import shlex

from .task_store import StoreError


REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV = REPO_ROOT / ".env"


def read_settings(names, env_file=DOTENV, environ=None):
    """Environment takes precedence; unrelated dotenv values are ignored.

    Supports KEY=value and quoted single-line values with optional comments.
    There is intentionally no shell expansion, interpolation or multiline parser.
    """
    environment = os.environ if environ is None else environ
    wanted = set(names)
    values = {}
    path = Path(env_file) if env_file is not None else None
    if path is not None and path.is_file():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
            if not match or match[1] not in wanted or match[1] in environment:
                continue
            try:
                tokens = shlex.split(match[2], comments=True, posix=True)
            except ValueError:
                raise StoreError(f"配置 {match[1]} 在第 {number} 行格式无效；值未输出") from None
            if len(tokens) > 1:
                raise StoreError(f"配置 {match[1]} 包含未加引号的空格；值未输出")
            values[match[1]] = tokens[0] if tokens else ""
    for name in wanted:
        if name in environment:
            values[name] = environment[name]
    return values


def image_model(explicit=None, env_file=DOTENV):
    if explicit is not None:
        return explicit
    settings = read_settings(["GEMINI_IMAGE_MODEL"], env_file)
    return settings.get("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"


def reference_project(mode, env_file=DOTENV):
    """Keep real project identifiers in local configuration, never in source.

    Offline records use a synthetic namespace and do not read local settings.
    Generation records remain restricted to the configured reference project.
    """
    if mode == "offline":
        return "offline-fixture"
    if mode != "generation":
        raise StoreError("模式须为 offline/generation")
    project = read_settings(["ANIGC_REFERENCE_PROJECT_ID"], env_file).get("ANIGC_REFERENCE_PROJECT_ID", "")
    if not project:
        raise StoreError("正式任务需要在本地配置 ANIGC_REFERENCE_PROJECT_ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", project):
        raise StoreError("ANIGC_REFERENCE_PROJECT_ID 格式无效；值未输出")
    return project


def gemini_key(env_file=DOTENV):
    settings = read_settings(["GEMINI_API_KEY", "GOOGLE_API_KEY"], env_file)
    # Prefer a process-provided key even if the file uses the other alias.
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
           or settings.get("GEMINI_API_KEY") or settings.get("GOOGLE_API_KEY"))
    if not key or not key.strip():
        raise StoreError("缺少 GEMINI_API_KEY（或 GOOGLE_API_KEY）；请在本地配置")
    if any(ord(char) < 32 or ord(char) > 126 for char in key):
        raise StoreError("API key 包含无效的 HTTP 头字符；值未输出")
    return key


def gpt_key(env_file=DOTENV):
    key = read_settings(["GPT_API_KEY"], env_file).get("GPT_API_KEY", "")
    if not key or not key.strip():
        raise StoreError("缺少 GPT_API_KEY；请在本地配置")
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise StoreError("GPT API key 包含无效的 HTTP 头字符；值未输出")
    return key
