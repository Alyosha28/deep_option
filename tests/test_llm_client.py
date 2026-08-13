"""LLM 客户端测试：.env 解析/优先级、重试、鉴权不重试、超时、脱敏、token 估算。"""

from __future__ import annotations

import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from src.agents.llm_client import (
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_REASONER_MODEL,
    LLMClient,
    LLMError,
    LLMSettings,
    create_client,
    estimate_tokens,
    load_settings,
    parse_dotenv,
    redact_secrets,
)


class FakeResponse:
    def __init__(self, payload: object):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self.payload
        return self.payload[:n]


class FakeOpener:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.requests: list[tuple[object, float]] = []

    def open(self, request: object, timeout: float | None = None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.deepseek.com/v1/chat/completions",
        code,
        "boom",
        {"Content-Type": "application/json"},
        io.BytesIO(body),
    )


def _ok_payload(content: str) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "model": "deepseek-chat",
    }


class ParseDotenvTests(unittest.TestCase):
    def test_parses_key_value_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# 注释\n"
                "DEEPSEEK_API_KEY=sk-abc\n"
                "export GOAI_CHAT_MODEL=deepseek-chat\n"
                "GOAI_CHAT_TIMEOUT_S=\"12.5\"\n"
                "EMPTY=\n",
                encoding="utf-8",
            )
            values = parse_dotenv(path)

        self.assertEqual(values["DEEPSEEK_API_KEY"], "sk-abc")
        self.assertEqual(values["GOAI_CHAT_MODEL"], "deepseek-chat")
        self.assertEqual(values["GOAI_CHAT_TIMEOUT_S"], "12.5")
        self.assertEqual(values["EMPTY"], "")

    def test_missing_file_returns_empty(self):
        self.assertEqual(parse_dotenv(Path("no-such-file.env")), {})

    def test_bom_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KEY=VALUE\n", encoding="utf-8-sig")  # utf-8-sig 自带 BOM
            self.assertEqual(parse_dotenv(path), {"KEY": "VALUE"})


class LoadSettingsTests(unittest.TestCase):
    def test_defaults_without_env(self):
        settings = load_settings(env={}, env_path=Path("no-such.env"))

        self.assertIsNone(settings.api_key)
        self.assertEqual(settings.base_url, DEFAULT_BASE_URL)
        self.assertEqual(settings.chat_model, DEFAULT_CHAT_MODEL)
        self.assertEqual(settings.reasoner_model, DEFAULT_REASONER_MODEL)
        self.assertFalse(settings.configured)

    def test_process_env_beats_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("DEEPSEEK_API_KEY=from-file\n", encoding="utf-8")
            settings = load_settings(
                env={"DEEPSEEK_API_KEY": "from-env"}, env_path=path
            )

        self.assertEqual(settings.api_key, "from-env")
        self.assertTrue(settings.configured)

    def test_invalid_numerics_fall_back_to_defaults(self):
        settings = load_settings(
            env={
                "GOAI_CHAT_TIMEOUT_S": "abc",
                "GOAI_REASONER_TIMEOUT_S": "-5",
                "GOAI_LLM_RETRIES": "x",
                "GOAI_LLM_RETRY_BACKOFF_S": "0",
            },
            env_path=Path("no-such.env"),
        )

        self.assertEqual(settings.chat_timeout_s, 30.0)
        self.assertEqual(settings.reasoner_timeout_s, 90.0)
        self.assertEqual(settings.retries, 2)
        self.assertEqual(settings.retry_backoff_s, 0.5)


class TokenAndSecretTests(unittest.TestCase):
    def test_estimate_tokens_mixed(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("腾讯 业绩"), 4)  # 2 CJK + 2 words
        self.assertEqual(estimate_tokens("hello world"), 2)

    def test_redact_secrets(self):
        secrets = ("sk-super-secret-key",)
        self.assertEqual(
            redact_secrets("key=sk-super-secret-key end", secrets),
            "key=[REDACTED] end",
        )
        self.assertEqual(redact_secrets("abc", ("abc",)), "abc")  # 过短不替换
        self.assertEqual(redact_secrets("clean text", secrets), "clean text")


class LLMClientChatTests(unittest.TestCase):
    def _client(self, responses: list[object], **settings) -> tuple[LLMClient, FakeOpener]:
        opener = FakeOpener(responses)
        base = LLMSettings(api_key="sk-test-key-1234", retries=2, retry_backoff_s=0.01)
        merged = LLMSettings(
            api_key=settings.get("api_key", base.api_key),
            base_url=settings.get("base_url", base.base_url),
            retries=settings.get("retries", base.retries),
            retry_backoff_s=settings.get("retry_backoff_s", base.retry_backoff_s),
        )
        return LLMClient(merged, opener=opener), opener

    def test_success_parses_response(self):
        client, opener = self._client([FakeResponse(_ok_payload("你好"))])

        result = client.chat([{"role": "user", "content": "hi"}], timeout=5)

        self.assertEqual(result["content"], "你好")
        self.assertEqual(result["usage"]["total_tokens"], 30)
        self.assertEqual(len(opener.requests), 1)

    def test_http_500_retries_then_raises(self):
        client, opener = self._client(
            [_http_error(500), _http_error(502), _http_error(503)]
        )
        with mock.patch("src.agents.llm_client.time.sleep") as sleep:
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(ctx.exception.kind, "http")
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_http_429_retries_then_succeeds(self):
        client, opener = self._client(
            [_http_error(429), FakeResponse(_ok_payload("ok"))]
        )
        with mock.patch("src.agents.llm_client.time.sleep") as sleep:
            result = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(result["content"], "ok")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(sleep.call_count, 1)

    def test_http_401_not_retried(self):
        client, opener = self._client([_http_error(401)])

        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(opener.requests), 1)

    def test_timeout_raises_without_retry(self):
        client, opener = self._client([socket.timeout("slow")])

        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertEqual(len(opener.requests), 1)

    def test_network_error_retries_then_raises(self):
        client, opener = self._client(
            [urllib.error.URLError("dns"), urllib.error.URLError("dns"), urllib.error.URLError("dns")]
        )
        with mock.patch("src.agents.llm_client.time.sleep"):
            with self.assertRaises(LLMError) as ctx:
                client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(ctx.exception.kind, "network")
        self.assertEqual(len(opener.requests), 3)

    def test_invalid_json_response(self):
        response = FakeResponse({"choices": []})
        client, _ = self._client([response])
        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.kind, "invalid_response")

    def test_message_validation(self):
        client, _ = self._client([FakeResponse(_ok_payload("ok"))])

        with self.assertRaises(LLMError):
            client.chat([])
        with self.assertRaises(LLMError):
            client.chat([{"role": "system", "content": 1}])
        with self.assertRaises(LLMError):
            client.chat([{"role": "bad", "content": "x"}])
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "x" * (64 * 1024 + 1)}])


class CreateClientTests(unittest.TestCase):
    def test_no_key_returns_none(self):
        self.assertIsNone(create_client(LLMSettings(api_key=None)))

    def test_key_returns_client(self):
        client = create_client(LLMSettings(api_key="sk-x"))
        self.assertIsNotNone(client)
        self.assertEqual(client.settings.api_key, "sk-x")


if __name__ == "__main__":
    unittest.main()
