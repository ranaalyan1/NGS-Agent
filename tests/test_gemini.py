"""Unit tests for Gemini LLM backend."""

import pytest
from ngs_agent.backends.gemini import GeminiBackend


class TestGeminiBackend:
    def test_missing_api_key_raises_runtime_error(self):
        backend = GeminiBackend(api_key="")
        with pytest.raises(RuntimeError) as exc:
            backend.complete("Test prompt")
        assert "Gemini API key not found" in str(exc.value)

    def test_backend_custom_model(self):
        backend = GeminiBackend(api_key="mock_key", model="gemini-1.5-pro")
        assert backend.model == "gemini-1.5-pro"
        assert backend.api_key == "mock_key"
