"""OpenAI 兼容 LLM 客户端（默认 DeepSeek，零新增依赖）。

用标准库 urllib 调用 ``POST {base_url}/chat/completions``：
- chat 默认超时 30s，reasoner 默认 90s；
- 5xx / 429 / 瞬时网络错误最多重试 2 次，401/403 等鉴权错误不重试；
- 密钥只从环境变量或 gitignored ``.env`` 读取，不落盘到日志或审计；
- 错误信息不携带响应原文与密钥片段。

铁律：本模块只负责传输与解析文本；数字、verdict、门控全部由自研引擎产出。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_CHAT_MODEL = "deepseek-chat"
DEFAULT_REASONER_MODEL = "deepseek-reasoner"
DEFAULT_CHAT_TIMEOUT_S = 30.0
DEFAULT_REASONER_TIMEOUT_S = 90.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF_S = 0.5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MESSAGES = 64
MAX_MESSAGE_CHARS = 64 * 1024

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ENV_VALUES = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "GOAI_CHAT_MODEL",
    "GOAI_REASONER_MODEL",
    "GOAI_CHAT_TIMEOUT_S",
    "GOAI_REASONER_TIMEOUT_S",
    "GOAI_LLM_RETRIES",
    "GOAI_LLM_RETRY_BACKOFF_S",
}


class LLMError(Exception):
    """类型化 LLM 调用失败，错误文案永远不含密钥与响应原文。"""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status: int | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retriable = retriable


@dataclass(frozen=True)
class LLMSettings:
    """LLM 供应商配置快照（frozen，线程安全）。"""

    api_key: str | None
    base_url: str = DEFAULT_BASE_URL
    chat_model: str = DEFAULT_CHAT_MODEL
    reasoner_model: str = DEFAULT_REASONER_MODEL
    chat_timeout_s: float = DEFAULT_CHAT_TIMEOUT_S
    reasoner_timeout_s: float = DEFAULT_REASONER_TIMEOUT_S
    retries: int = DEFAULT_RETRIES
    retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S

    @property
    def configured(self) -> bool:
        return bool(self.api_key and str(self.api_key).strip())


def _as_float(value: Any, default: float, *, positive: bool = True) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if positive and parsed <= 0:
        return default
    return parsed


def _as_int(value: Any, default: int, *, nonnegative: bool = True) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if nonnegative and parsed < 0:
        return default
    return parsed


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """解析极简 ``KEY=VALUE`` 环境文件；不引入 python-dotenv 依赖。"""

    values: dict[str, str] = {}
    dotenv_path = Path(path)
    if not dotenv_path.is_file():
        return values
    try:
        lines = dotenv_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_settings(
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
) -> LLMSettings:
    """读取 LLM 配置；真实环境变量优先于 ``.env`` 文件。"""

    process_env = dict(os.environ) if env is None else dict(env)
    dotenv = parse_dotenv(env_path if env_path is not None else ROOT / ".env")
    merged: dict[str, str] = {}
    for key in _ENV_VALUES:
        value = process_env.get(key, dotenv.get(key))
        if value is not None:
            merged[key] = value

    def pick(name: str, default: str | None) -> str | None:
        value = merged.get(name)
        return value if value is not None and value.strip() else default

    return LLMSettings(
        api_key=pick("DEEPSEEK_API_KEY", None),
        base_url=pick("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
        chat_model=pick("GOAI_CHAT_MODEL", DEFAULT_CHAT_MODEL) or DEFAULT_CHAT_MODEL,
        reasoner_model=(
            pick("GOAI_REASONER_MODEL", DEFAULT_REASONER_MODEL)
            or DEFAULT_REASONER_MODEL
        ),
        chat_timeout_s=_as_float(
            merged.get("GOAI_CHAT_TIMEOUT_S"), DEFAULT_CHAT_TIMEOUT_S
        ),
        reasoner_timeout_s=_as_float(
            merged.get("GOAI_REASONER_TIMEOUT_S"), DEFAULT_REASONER_TIMEOUT_S
        ),
        retries=_as_int(merged.get("GOAI_LLM_RETRIES"), DEFAULT_RETRIES),
        retry_backoff_s=_as_float(
            merged.get("GOAI_LLM_RETRY_BACKOFF_S"), DEFAULT_RETRY_BACKOFF_S
        ),
    )


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（仅用于 UI 展示）：CJK 字符按 1，其余按空白分词。"""

    if not text:
        return 0
    cjk_count = len(_CJK.findall(text))
    remainder = _CJK.sub(" ", text)
    words = [word for word in re.split(r"\s+", remainder) if word]
    return cjk_count + len(words)


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    """把密钥片段替换为占位符；长度不足 4 的片段不参与替换（避免误伤）。"""

    result = str(text)
    for secret in secrets:
        fragment = str(secret).strip()
        if len(fragment) >= 4 and fragment in result:
            result = result.replace(fragment, "[REDACTED]")
    return result


def _validate_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(messages, (list, tuple)) or not messages:
        raise LLMError("invalid_request", "messages must be a non-empty list")
    if len(messages) > MAX_MESSAGES:
        raise LLMError("invalid_request", "too many messages")
    cleaned: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise LLMError("invalid_request", "each message must be an object")
        role = message.get("role")
        content = message.get("content")
        if role not in ("system", "user", "assistant"):
            raise LLMError("invalid_request", "unsupported message role")
        if not isinstance(content, str):
            raise LLMError("invalid_request", "message content must be a string")
        if len(content) > MAX_MESSAGE_CHARS:
            raise LLMError("invalid_request", "message content too long")
        cleaned.append({"role": role, "content": content})
    return cleaned


class LLMClient:
    """线程安全的 OpenAI 兼容 chat 客户端（每调用独立请求，无共享可变状态）。"""

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        opener: Any = None,
    ) -> None:
        self.settings = settings if settings is not None else load_settings()
        if not self.settings.configured:
            raise LLMError("no_key", "LLMClient requires a non-empty API key")
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._endpoint = (
            self.settings.base_url.rstrip("/") + "/chat/completions"
        )

    def _post(self, request: urllib.request.Request, timeout: float) -> Any:
        return self._opener.open(request, timeout=timeout)

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 900,
        timeout: float | None = None,
        labels: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 chat completions 并返回结构化结果；失败抛类型化 ``LLMError``。"""

        cleaned = _validate_messages(messages)
        selected_model = model or self.settings.chat_model
        request_timeout = (
            timeout if timeout is not None else self.settings.chat_timeout_s
        )
        body = {
            "model": selected_model,
            "messages": cleaned,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last_error: LLMError | None = None
        attempts = 1 + max(0, self.settings.retries)
        for attempt in range(attempts):
            request = urllib.request.Request(
                self._endpoint, data=raw_body, headers=headers, method="POST"
            )
            try:
                response = self._post(request, request_timeout)
            except (socket.timeout, TimeoutError) as exc:
                raise LLMError(
                    "timeout", "LLM 请求超时（未收到响应）", retriable=True
                ) from exc
            except urllib.error.HTTPError as exc:
                error = self._http_error(exc)
                last_error = error
                if error.retriable and attempt + 1 < attempts:
                    time.sleep(self.settings.retry_backoff_s * (2**attempt))
                    continue
                raise error
            except urllib.error.URLError as exc:
                error = LLMError(
                    "network", "LLM 网络不可达", retriable=True
                )
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(self.settings.retry_backoff_s * (2**attempt))
                    continue
                raise error from exc
            except OSError as exc:
                error = LLMError("network", "LLM 网络不可达", retriable=True)
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(self.settings.retry_backoff_s * (2**attempt))
                    continue
                raise error from exc
            try:
                return self._parse_response(response, selected_model, labels)
            except LLMError as exc:
                last_error = exc
                if exc.retriable and attempt + 1 < attempts:
                    time.sleep(self.settings.retry_backoff_s * (2**attempt))
                    continue
                raise
        raise last_error or LLMError("http", "LLM 请求失败")

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> LLMError:
        status = int(exc.code)
        if status in (401, 403):
            return LLMError(
                "auth", f"LLM 鉴权失败（HTTP {status}），请检查 API Key", status=status
            )
        if status == 429:
            return LLMError("rate_limited", "LLM 限流（HTTP 429）", status=status, retriable=True)
        if status >= 500:
            return LLMError(
                "http", f"LLM 服务端错误（HTTP {status}）", status=status, retriable=True
            )
        return LLMError("http", f"LLM 请求失败（HTTP {status}）", status=status)

    @staticmethod
    def _parse_response(
        response: Any,
        selected_model: str,
        labels: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LLMError("invalid_response", "LLM 响应过大")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError("invalid_response", "LLM 返回非 JSON 响应") from exc
        if not isinstance(payload, dict):
            raise LLMError("invalid_response", "LLM 返回结构非法")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("invalid_response", "LLM 响应缺少 choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMError("invalid_response", "LLM choices[0] 结构非法")
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LLMError("invalid_response", "LLM 响应缺少文本内容")
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return {
            "content": content,
            "model": str(payload.get("model") or selected_model),
            "finish_reason": first.get("finish_reason"),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            "labels": dict(labels) if labels is not None else {},
        }


def create_client(settings: LLMSettings | None = None) -> LLMClient | None:
    """有密钥时构造客户端；无密钥返回 None（调用方走确定性回退）。"""

    resolved = settings if settings is not None else load_settings()
    if not resolved.configured:
        return None
    return LLMClient(resolved)


__all__ = [
    "LLMClient",
    "LLMError",
    "LLMSettings",
    "create_client",
    "estimate_tokens",
    "load_settings",
    "parse_dotenv",
    "redact_secrets",
]
