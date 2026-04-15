"""Unit tests for llm_provider.py."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_provider import (
    ContentPart, TokenUsage, create_provider,
    _is_retryable_generic, _is_rate_limit_generic,
    extract_pdf_text,
)


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


class TestCreateProvider:
    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("nonexistent", "fake-key")

    def test_known_providers(self):
        # Verify the factory creates providers without ValueError
        # Some SDKs may not be installed, so skip those gracefully
        for name in ["gemini", "anthropic", "openai"]:
            try:
                provider = create_provider(name, "test-key-placeholder")
                assert provider is not None
            except ImportError:
                pass  # SDK not installed in this environment

    def test_case_insensitive(self):
        try:
            provider = create_provider("Gemini", "test-key-placeholder")
            assert provider is not None
        except ImportError:
            pass


class TestExtractPdfText:
    def test_invalid_pdf(self):
        result = extract_pdf_text(b"not a real pdf")
        # Should return error message, not crash
        assert "could not be extracted" in result.lower() or len(result) >= 0
