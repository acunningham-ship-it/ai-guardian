"""Tests for v2 features: smart_tokens, semantic cache, savings tracking."""
import pytest

from guardian.cost.smart_tokens import (
    classify_task, compute_smart_max_tokens, TaskType, TASK_MAX_TOKENS,
)
from guardian.cache.semantic import compute_cache_key, _normalize_messages


# ── Smart Max Tokens ────────────────────────────────────────────────

class TestTaskClassification:
    def test_summarize_detected(self):
        messages = [{"role": "user", "content": "Summarize this article about AI"}]
        assert classify_task(messages) == TaskType.SUMMARIZE

    def test_tldr_detected(self):
        messages = [{"role": "user", "content": "TLDR this thread"}]
        assert classify_task(messages) == TaskType.SUMMARIZE

    def test_list_detected(self):
        messages = [{"role": "user", "content": "List all the reasons why startups fail"}]
        assert classify_task(messages) == TaskType.LIST

    def test_translate_detected(self):
        messages = [{"role": "user", "content": "Translate this to Spanish"}]
        assert classify_task(messages) == TaskType.TRANSLATE

    def test_explain_detected(self):
        messages = [{"role": "user", "content": "Explain how transformers work"}]
        assert classify_task(messages) == TaskType.EXPLAIN

    def test_code_fix_detected(self):
        messages = [
            {"role": "user", "content": "Fix this bug"},
            {"role": "assistant", "content": "```python\ndef foo():\n    pass\n```"},
        ]
        assert classify_task(messages) == TaskType.CODE_FIX

    def test_code_write_detected(self):
        messages = [{"role": "user", "content": "Write a function to sort a list"}]
        assert classify_task(messages) == TaskType.CODE_WRITE

    def test_code_review_detected(self):
        messages = [
            {"role": "user", "content": "Review this code for security issues"},
            {"role": "assistant", "content": "```python\napp.run(debug=True)\n```"},
        ]
        assert classify_task(messages) == TaskType.CODE_REVIEW

    def test_architect_detected(self):
        messages = [{"role": "user", "content": "Design a system for real-time chat"}]
        assert classify_task(messages) == TaskType.ARCHITECT

    def test_answer_detected(self):
        messages = [{"role": "user", "content": "What is the capital of France?"}]
        assert classify_task(messages) == TaskType.ANSWER

    def test_chat_fallback(self):
        messages = [{"role": "user", "content": "Hello!"}]
        assert classify_task(messages) == TaskType.CHAT

    def test_empty_messages(self):
        assert classify_task([]) == TaskType.UNKNOWN

    def test_no_user_messages(self):
        messages = [{"role": "system", "content": "You are helpful"}]
        assert classify_task(messages) == TaskType.UNKNOWN


class TestSmartMaxTokens:
    def test_summarize_gets_low_max(self):
        messages = [{"role": "user", "content": "Summarize this document"}]
        result = compute_smart_max_tokens(messages, default_max_tokens=4096)
        assert result["max_tokens"] == 400  # TASK_MAX_TOKENS[summarize]
        assert result["task_type"] == TaskType.SUMMARIZE
        assert result["changed"] is True
        assert result["tokens_saved"] > 0

    def test_architect_gets_high_max(self):
        messages = [{"role": "user", "content": "Design a system for distributed caching"}]
        result = compute_smart_max_tokens(messages, default_max_tokens=4096)
        assert result["max_tokens"] == 4000
        assert result["task_type"] == TaskType.ARCHITECT

    def test_respects_client_max_when_lower(self):
        messages = [{"role": "user", "content": "Summarize this"}]
        result = compute_smart_max_tokens(messages, requested_max_tokens=200, default_max_tokens=4096)
        assert result["max_tokens"] == 200  # Client asked for less

    def test_no_change_when_enforce_false_and_client_specified(self):
        messages = [{"role": "user", "content": "Summarize this"}]
        result = compute_smart_max_tokens(
            messages, requested_max_tokens=1000, default_max_tokens=4096, enforce=False
        )
        assert result["max_tokens"] == 1000  # Keep client value when enforce=False
        assert result["changed"] is False

    def test_all_task_types_have_max_tokens(self):
        """Ensure every TaskType has a corresponding max_tokens entry."""
        for attr in dir(TaskType):
            if not attr.startswith("_") and attr != "UNKNOWN":
                value = getattr(TaskType, attr)
                assert value in TASK_MAX_TOKENS, f"Missing max_tokens for TaskType.{attr}"


# ── Semantic Cache Key ──────────────────────────────────────────────

class TestCacheKey:
    def test_same_request_same_key(self):
        messages = [{"role": "user", "content": "Hello world"}]
        key1 = compute_cache_key(messages, "gpt-4o", 1.0, 100)
        key2 = compute_cache_key(messages, "gpt-4o", 1.0, 100)
        assert key1 == key2

    def test_different_content_different_key(self):
        messages1 = [{"role": "user", "content": "Hello world"}]
        messages2 = [{"role": "user", "content": "Goodbye world"}]
        key1 = compute_cache_key(messages1, "gpt-4o", 1.0, 100)
        key2 = compute_cache_key(messages2, "gpt-4o", 1.0, 100)
        assert key1 != key2

    def test_different_model_different_key(self):
        messages = [{"role": "user", "content": "Hello world"}]
        key1 = compute_cache_key(messages, "gpt-4o", 1.0, 100)
        key2 = compute_cache_key(messages, "claude-sonnet-4", 1.0, 100)
        assert key1 != key2

    def test_whitespace_normalization(self):
        messages1 = [{"role": "user", "content": "Hello   world"}]
        messages2 = [{"role": "user", "content": "Hello world"}]
        key1 = compute_cache_key(messages1, "gpt-4o", 1.0, 100)
        key2 = compute_cache_key(messages2, "gpt-4o", 1.0, 100)
        assert key1 == key2  # Normalized to same

    def test_key_is_64_char_hex(self):
        messages = [{"role": "user", "content": "Test"}]
        key = compute_cache_key(messages, "gpt-4o", 1.0, 100)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


# ── Failover ────────────────────────────────────────────────────────

class TestFailoverChains:
    def test_anthropic_has_fallbacks(self):
        from guardian.cost.failover import get_fallback_chain
        chain = get_fallback_chain("anthropic/claude-sonnet-4")
        assert len(chain) >= 2
        assert "openai/gpt-4o" in chain

    def test_openai_has_fallbacks(self):
        from guardian.cost.failover import get_fallback_chain
        chain = get_fallback_chain("openai/gpt-4o")
        assert len(chain) >= 2
        assert "anthropic/claude-sonnet-4" in chain

    def test_unknown_model_no_fallbacks(self):
        from guardian.cost.failover import get_fallback_chain
        chain = get_fallback_chain("some-unknown-model")
        assert chain == []

    def test_custom_fallbacks_override(self):
        from guardian.cost.failover import get_fallback_chain
        custom = {"openai/gpt-4o": ["anthropic/claude-haiku-4"]}
        chain = get_fallback_chain("openai/gpt-4o", custom_fallbacks=custom)
        assert chain == ["anthropic/claude-haiku-4"]
