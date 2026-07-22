"""Unit tests for shared helpers - _extract_json, _normalize_verdict, _normalize_params."""

import json
import pytest


class TestExtractJson:
    """Test JSON extraction from text (duplicated helper in multiple agents)."""

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Copy of the helper from agents - will be DRY'd in Phase 2."""
        import re
        if not text:
            return None
        for candidate in [text.strip()] + [
            m.group(0) for m in [re.search(r"\{[\s\S]*\}", text)] if m
        ]:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
        return None

    def test_clean_json_object(self):
        """Clean JSON should parse directly."""
        text = '{"key": "value", "number": 42}'
        result = self._extract_json(text)
        assert result is not None
        assert result["key"] == "value"
        assert result["number"] == 42
    
    def test_json_with_surrounding_text(self):
        """JSON embedded in text should be extracted via regex."""
        text = '''Here's the result: {"verdict": "pass", "confidence": 0.95}. Hope that helps!'''
        result = self._extract_json(text)
        assert result is not None
        assert result["verdict"] == "pass"
    
    def test_json_with_markdown_fences(self):
        """JSON in markdown code blocks should be extracted."""
        text = '''```json
{"status": "ok", "data": [1, 2, 3]}
```'''
        result = self._extract_json(text)
        assert result is not None
        assert result["status"] == "ok"
    
    def test_invalid_json_returns_none(self):
        """Invalid JSON should return None."""
        text = '{"broken": json}'
        result = self._extract_json(text)
        assert result is None
    
    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        assert self._extract_json("") is None
        assert self._extract_json(None) is None  # type: ignore
    
    def test_non_dict_json_returns_none(self):
        """JSON array or primitive should return None (we want dicts)."""
        assert self._extract_json("[1, 2, 3]") is None
        assert self._extract_json('"just a string"') is None


class TestNormalizeVerdict:
    """Test verdict normalization (from QC agent)."""

    @staticmethod
    def _normalize_verdict(raw: str) -> str | None:
        """Copy from QCAgent - fuzzy-map to canonical values."""
        v = raw.strip().lower().replace("-", "_").replace(" ", "_")
        
        if v in ("pass", "trim_required", "fail"):
            return v
        
        if v in ("fail", "failed", "unusable"):
            return "fail"
        if "trim" in v:
            return "trim_required"
        if v in ("passed", "good", "ok", "okay", "accept", "accepted"):
            return "pass"
        
        return None

    def test_canonical_values_pass_through(self):
        """Canonical values should pass through unchanged."""
        assert self._normalize_verdict("pass") == "pass"
        assert self._normalize_verdict("trim_required") == "trim_required"
        assert self._normalize_verdict("fail") == "fail"
    
    def test_variants_normalized_to_pass(self):
        """Various synonyms should normalize to 'pass'."""
        assert self._normalize_verdict("passed") == "pass"
        assert self._normalize_verdict("good") == "pass"
        assert self._normalize_verdict("OK") == "pass"
        assert self._normalize_verdict("accepted") == "pass"
    
    def test_trim_variants_normalized(self):
        """Trim-related verdicts should normalize to 'trim_required'."""
        assert self._normalize_verdict("trim_needed") == "trim_required"
        assert self._normalize_verdict("trimming_recommended") == "trim_required"
        assert self._normalize_verdict("needs_trim") == "trim_required"
    
    def test_fail_variants_normalized(self):
        """Fail synonyms should normalize to 'fail'."""
        assert self._normalize_verdict("failed") == "fail"
        assert self._normalize_verdict("unusable") == "fail"
    
    def test_unrecognized_returns_none(self):
        """Unrecognized verdicts should return None."""
        assert self._normalize_verdict("unknown_verdict") is None
        # Note: "maybe_trim" contains "trim" so it normalizes to trim_required
        # This is expected behavior of the substring matching


class TestNormalizeParams:
    """Test trim parameter normalization (from AI decider agent)."""

    DEFAULT_TRIM_PARAMS = {
        "LEADING": 3,
        "TRAILING": 3,
        "SLIDINGWINDOW": "4:20",
        "MINLEN": 36,
    }

    @staticmethod
    def _normalize_params(params: dict) -> dict:
        """Copy from AIDeciderAgent - clamp and validate trim params."""
        import re
        merged = {**TestNormalizeParams.DEFAULT_TRIM_PARAMS, **(params or {})}
        
        try:
            merged["LEADING"] = max(0, min(40, int(merged["LEADING"])))
            merged["TRAILING"] = max(0, min(40, int(merged["TRAILING"])))
            merged["MINLEN"] = max(36, min(200, int(merged["MINLEN"])))
        except Exception:
            return dict(TestNormalizeParams.DEFAULT_TRIM_PARAMS)
        
        sw = str(merged["SLIDINGWINDOW"])
        if not re.match(r"^\d+:\d+$", sw):
            sw = TestNormalizeParams.DEFAULT_TRIM_PARAMS["SLIDINGWINDOW"]
        merged["SLIDINGWINDOW"] = sw
        return merged

    def test_default_params_unchanged(self):
        """Default params should remain unchanged."""
        result = self._normalize_params({})
        assert result == self.DEFAULT_TRIM_PARAMS
    
    def test_custom_params_applied(self):
        """Custom params should override defaults."""
        result = self._normalize_params({"LEADING": 10, "TRAILING": 10})
        assert result["LEADING"] == 10
        assert result["TRAILING"] == 10
        assert result["MINLEN"] == 36  # default
    
    def test_params_clamped_to_valid_range(self):
        """Out-of-range params should be clamped."""
        result = self._normalize_params({"LEADING": 100, "MINLEN": 10})
        assert result["LEADING"] == 40  # max
        assert result["MINLEN"] == 36   # min
    
    def test_invalid_slidingwindow_resets_to_default(self):
        """Invalid SLIDINGWINDOW format should reset to default."""
        result = self._normalize_params({"SLIDINGWINDOW": "invalid"})
        assert result["SLIDINGWINDOW"] == "4:20"
    
    def test_valid_slidingwindow_accepted(self):
        """Valid SLIDINGWINDOW format should be accepted."""
        result = self._normalize_params({"SLIDINGWINDOW": "5:25"})
        assert result["SLIDINGWINDOW"] == "5:25"
