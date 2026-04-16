"""Unit tests for llm_provider.py."""

import pytest
import sys
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field

from llm_provider import (
    ContentPart, TokenUsage, LLMProvider, create_provider,
    _is_retryable_generic, _is_rate_limit_generic,
    extract_pdf_text, _api_semaphore,
)


# --- Test schema for structured output ---
class MockReview(BaseModel):
    verdict: str = Field(description="Verdict")
    summary: str = Field(default="", description="Summary")


# --- Dataclass Tests ---

class TestContentPart:
    def test_text_part(self):
        part = ContentPart(type="text", data="hello world")
        assert part.type == "text"
        assert part.data == "hello world"

    def test_pdf_part(self):
        part = ContentPart(type="pdf", data=b"%PDF-1.4", mime_type="application/pdf")
        assert part.type == "pdf"
        assert isinstance(part.data, bytes)
        assert part.mime_type == "application/pdf"

    def test_default_mime_type(self):
        part = ContentPart(type="text", data="test")
        assert part.mime_type == ""


class TestTokenUsage:
    def test_defaults(self):
        usage = TokenUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.thinking_tokens == 0

    def test_custom_values(self):
        usage = TokenUsage(input_tokens=100, output_tokens=50, thinking_tokens=25)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.thinking_tokens == 25


# --- Retry Logic ---

class TestRetryLogic:
    def test_rate_limit(self):
        assert _is_retryable_generic(Exception("429 rate limit")) is True
        assert _is_rate_limit_generic(Exception("429 rate limit")) is True

    def test_server_error(self):
        assert _is_retryable_generic(Exception("500 Internal Server Error")) is True
        assert _is_rate_limit_generic(Exception("500 Internal Server Error")) is False

    def test_client_error_not_retryable(self):
        assert _is_retryable_generic(Exception("400 Bad Request")) is False

    def test_overloaded(self):
        assert _is_retryable_generic(Exception("overloaded")) is True

    def test_resource_exhausted(self):
        assert _is_retryable_generic(Exception("resource_exhausted")) is True
        assert _is_rate_limit_generic(Exception("resource_exhausted")) is True

    def test_502_gateway(self):
        assert _is_retryable_generic(Exception("502 Bad Gateway")) is True

    def test_capacity(self):
        assert _is_retryable_generic(Exception("capacity")) is True

    def test_auth_error_not_retryable(self):
        assert _is_retryable_generic(Exception("403 Forbidden")) is False


# --- Factory ---

class TestCreateProvider:
    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("nonexistent", "fake-key")

    def test_known_providers(self):
        for name in ["gemini", "anthropic", "openai"]:
            try:
                provider = create_provider(name, "test-key-placeholder")
                assert provider is not None
                assert provider.name  # has a name property
            except ImportError:
                pass  # SDK not installed

    def test_case_insensitive(self):
        try:
            provider = create_provider("Gemini", "test-key-placeholder")
            assert provider is not None
        except ImportError:
            pass

    def test_whitespace_stripped(self):
        try:
            provider = create_provider("  gemini  ", "test-key-placeholder")
            assert provider is not None
        except ImportError:
            pass


# --- Extract PDF Text ---

class TestExtractPdfText:
    def test_invalid_pdf(self):
        result = extract_pdf_text(b"not a real pdf")
        # Should return an error message, not crash
        assert "could not be extracted" in result.lower() or "extraction failed" in result.lower()

    def test_empty_bytes(self):
        result = extract_pdf_text(b"")
        assert isinstance(result, str)


# --- LLMProvider Base (generate_structured with retry) ---

class ConcreteProvider(LLMProvider):
    """Test provider that delegates to a configurable callable."""

    def __init__(self, generate_fn=None, retryable_fn=None, **kwargs):
        super().__init__(**kwargs)
        self._generate_fn = generate_fn
        self._retryable_fn = retryable_fn

    def _generate_once(self, model, contents, schema, thinking_budget=None, cache_name=None):
        if self._generate_fn:
            return self._generate_fn(model, contents, schema, thinking_budget, cache_name)
        return schema(verdict="Approved"), TokenUsage(input_tokens=10, output_tokens=5)

    def _is_retryable(self, error):
        if self._retryable_fn:
            return self._retryable_fn(error)
        return super()._is_retryable(error)

    @property
    def name(self):
        return "ConcreteTestProvider"


class TestGenerateStructured:
    def test_success_first_attempt(self):
        provider = ConcreteProvider()
        result, usage = provider.generate_structured(
            "test-model",
            [ContentPart(type="text", data="test")],
            MockReview,
        )
        assert result.verdict == "Approved"
        assert usage.input_tokens == 10

    def test_retry_on_retryable_error(self):
        call_count = 0

        def flaky_generate(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("503 Service Unavailable")
            return schema(verdict="OK"), TokenUsage()

        provider = ConcreteProvider(generate_fn=flaky_generate, max_retries=3)
        with patch("llm_provider.time.sleep"):  # skip actual sleep
            result, _ = provider.generate_structured(
                "test-model",
                [ContentPart(type="text", data="test")],
                MockReview,
            )
        assert result.verdict == "OK"
        assert call_count == 2

    def test_no_retry_on_non_retryable_error(self):
        call_count = 0

        def failing_generate(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            raise Exception("400 Bad Request")

        provider = ConcreteProvider(generate_fn=failing_generate, max_retries=3)
        with pytest.raises(Exception, match="400 Bad Request"):
            provider.generate_structured(
                "test-model",
                [ContentPart(type="text", data="test")],
                MockReview,
            )
        assert call_count == 1  # no retry

    def test_raises_after_max_retries(self):
        call_count = 0

        def always_fail(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            raise Exception("503 Service Unavailable")

        provider = ConcreteProvider(generate_fn=always_fail, max_retries=3)
        with patch("llm_provider.time.sleep"):
            with pytest.raises(Exception, match="503"):
                provider.generate_structured(
                    "test-model",
                    [ContentPart(type="text", data="test")],
                    MockReview,
                )
        assert call_count == 3

    def test_rate_limit_uses_longer_wait(self):
        """Rate limit errors should use at least 15s wait."""
        waits = []

        def track_sleep(seconds):
            waits.append(seconds)

        call_count = 0

        def rate_limited(model, contents, schema, thinking_budget, cache_name):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("429 rate limit exceeded")
            return schema(verdict="OK"), TokenUsage()

        provider = ConcreteProvider(generate_fn=rate_limited, max_retries=3)
        with patch("llm_provider.time.sleep", side_effect=track_sleep):
            result, _ = provider.generate_structured(
                "test-model",
                [ContentPart(type="text", data="test")],
                MockReview,
            )
        assert result.verdict == "OK"
        assert all(w >= 15 for w in waits)  # rate limit waits are >= 15s


# --- Provider-Specific _is_retryable ---

class TestAnthropicRetryable:
    def test_529_overloaded(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("529 overloaded")) is True
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_normal_error_delegates_to_generic(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("400 Bad Request")) is False
            assert provider._is_retryable(Exception("503 error")) is True
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestOpenAIRetryable:
    def test_insufficient_quota_not_retryable(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("insufficient_quota")) is False
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_server_error_retryable(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            provider.max_retries = 3
            provider._thinking_warned = False
            assert provider._is_retryable(Exception("500 server error")) is True
        except ImportError:
            pytest.skip("openai SDK not installed")


# --- Content Conversion ---

class TestGeminiContentConversion:
    def test_text_content(self):
        try:
            from llm_provider import GeminiProvider
            provider = GeminiProvider.__new__(GeminiProvider)
            from google.genai import types
            provider._types = types
            parts = [ContentPart(type="text", data="hello")]
            native = provider._to_native_contents(parts)
            assert native == ["hello"]
        except ImportError:
            pytest.skip("google-genai SDK not installed")

    def test_unknown_type_skipped(self):
        try:
            from llm_provider import GeminiProvider
            provider = GeminiProvider.__new__(GeminiProvider)
            from google.genai import types
            provider._types = types
            parts = [
                ContentPart(type="text", data="hello"),
                ContentPart(type="unknown", data="mystery"),
            ]
            native = provider._to_native_contents(parts)
            assert len(native) == 1  # unknown skipped
        except ImportError:
            pytest.skip("google-genai SDK not installed")


class TestAnthropicContentConversion:
    def test_text_content(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="text", data="hello")]
            blocks = provider._to_content_blocks(parts)
            assert blocks == [{"type": "text", "text": "hello"}]
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_text_with_cache(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [ContentPart(type="text", data="hello")]
            blocks = provider._to_content_blocks(parts, cache_name="__anthropic_prompt_cache__")
            assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_unknown_type_skipped(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            parts = [
                ContentPart(type="text", data="hello"),
                ContentPart(type="unknown", data="mystery"),
            ]
            blocks = provider._to_content_blocks(parts)
            assert len(blocks) == 1
        except ImportError:
            pytest.skip("anthropic SDK not installed")


class TestOpenAIContentConversion:
    def test_text_content(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [ContentPart(type="text", data="hello")]
            messages = provider._to_messages(parts)
            assert messages == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_pdf_fallback_to_text(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [ContentPart(type="pdf", data=b"fake pdf data")]
            messages = provider._to_messages(parts)
            # Should convert to text block via extract_pdf_text fallback
            assert messages[0]["content"][0]["type"] == "text"
            assert "[Extracted from PDF]" in messages[0]["content"][0]["text"]
        except ImportError:
            pytest.skip("openai SDK not installed")

    def test_unknown_type_skipped(self):
        try:
            from llm_provider import OpenAIProvider
            provider = OpenAIProvider.__new__(OpenAIProvider)
            parts = [
                ContentPart(type="text", data="hello"),
                ContentPart(type="unknown", data="mystery"),
            ]
            messages = provider._to_messages(parts)
            assert len(messages[0]["content"]) == 1
        except ImportError:
            pytest.skip("openai SDK not installed")


# --- Cache Default Implementations ---

class TestCacheDefaults:
    def test_default_create_cache_returns_none(self):
        provider = ConcreteProvider()
        assert provider.create_cache("model", []) is None

    def test_default_delete_cache_is_noop(self):
        provider = ConcreteProvider()
        provider.delete_cache("some-cache")  # should not raise

    def test_anthropic_cache_returns_sentinel(self):
        try:
            from llm_provider import AnthropicProvider
            provider = AnthropicProvider.__new__(AnthropicProvider)
            result = provider.create_cache("model", [])
            assert result == "__anthropic_prompt_cache__"
        except ImportError:
            pytest.skip("anthropic SDK not installed")
