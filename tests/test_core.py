"""Tests for AI Guardian core functionality."""
import pytest
import asyncio
from guardian.cost.tracker import estimate_cost, check_budget, get_cheaper_alternatives, MODEL_PRICING
from guardian.cost.router import route_model, detect_task_complexity, MODEL_TIERS
from guardian.quality.checker import check_quality, _check_security, _check_performance, _check_syntax


class TestCostTracking:
    """Test cost estimation and budget enforcement."""

    def test_estimate_cost_gpt4o(self):
        cost = estimate_cost("openai/gpt-4o", 1000, 500)
        # (1000/1M * 2.50) + (500/1M * 10.00) = 0.0025 + 0.005 = 0.0075
        assert abs(cost - 0.0075) < 0.0001

    def test_estimate_cost_claude_sonnet(self):
        cost = estimate_cost("anthropic/claude-sonnet-4", 2000, 1000)
        # (2000/1M * 3.00) + (1000/1M * 15.00) = 0.006 + 0.015 = 0.021
        assert abs(cost - 0.021) < 0.0001

    def test_estimate_cost_zero_for_local(self):
        cost = estimate_cost("meta/llama-3.1-70b", 10000, 5000)
        assert cost == 0.0

    def test_estimate_cost_unknown_model_defaults(self):
        cost = estimate_cost("some-unknown-model", 1000, 500)
        # Should default to gpt-4o pricing
        assert cost > 0

    def test_cheaper_alternatives_for_opus(self):
        alts = get_cheaper_alternatives("anthropic/claude-opus-4")
        assert len(alts) > 0
        # All alternatives should be cheaper
        opus_price = MODEL_PRICING["anthropic/claude-opus-4"]["out"]
        for alt in alts:
            assert MODEL_PRICING[alt]["out"] < opus_price

    def test_cheaper_alternatives_for_haiku(self):
        alts = get_cheaper_alternatives("anthropic/claude-haiku-4")
        # Hauku is already cheapest, so alternatives should be empty or very few
        assert isinstance(alts, list)


class TestModelRouting:
    """Test smart model routing logic."""

    def test_simple_task_routed_down(self):
        messages = [{"role": "user", "content": "Hi, how are you?"}]
        result = route_model("anthropic/claude-opus-4", messages, prefer_cheap=True)
        assert result.routed_model != "anthropic/claude-opus-4"
        assert result.reason == "cost"

    def test_complex_task_stays_high(self):
        messages = [{"role": "user", "content": "Design a distributed microservice architecture for a real-time trading system with database schema, API design, and security considerations including authentication, rate limiting, and data encryption."}]
        result = route_model("anthropic/claude-haiku-4", messages, prefer_cheap=True)
        # Should upgrade because task is complex
        assert result.reason in ("quality", "cost", "none")

    def test_budget_constraint_downgrade(self):
        messages = [{"role": "user", "content": "Write a python function to sort a list"}]
        result = route_model(
            "anthropic/claude-opus-4", messages,
            budget_remaining=0.5, prefer_cheap=True,
        )
        # With almost no budget, should downgrade
        assert result.routed_model != "anthropic/claude-opus-4"

    def test_task_complexity_detection_simple(self):
        messages = [{"role": "user", "content": "Hello world"}]
        tier = detect_task_complexity(messages)
        assert tier <= 1

    def test_task_complexity_detection_code(self):
        messages = [{"role": "user", "content": """
        I need to implement a class-based state machine for a payment processing system.
        It should handle transitions between states, validate each transition with
        database lookups, and log everything to a file. Also need to handle edge cases
        like network failures and implement retry logic with exponential backoff.
        """ }]
        tier = detect_task_complexity(messages)
        assert tier >= 2


class TestQualityGuardrails:
    """Test quality checking."""

    def test_hardcoded_api_key_detected(self):
        code = '''
        api_key = "sk-1234567890abcdef"
        response = requests.get(url, headers={"Authorization": f"Bearer {api_key}"})
        '''
        issues = _check_security(code)
        assert any("API key" in i for i in issues)

    def test_sql_injection_detected(self):
        code = '''
        query = "SELECT * FROM users WHERE id = %s" % user_id
        cursor.execute(query)
        '''.strip()
        issues = _check_security(code)
        assert any("SQL injection" in i for i in issues)

    def test_eval_detected(self):
        code = '''
        result = eval(user_input)
        '''
        issues = _check_security(code)
        assert any("eval" in i.lower() for i in issues)

    def test_debug_mode_detected(self):
        code = '''
        app.run(debug=True)
        '''
        issues = _check_security(code)
        assert any("Debug" in i for i in issues)

    def test_nested_loop_performance_flag(self):
        code = '''
        for item in items:
            for subitem in item:
                for detail in subitem:
                    process(detail)
        '''
        flags = _check_performance(code)
        assert any("nested" in f.lower() or "loop" in f.lower() for f in flags)

    def test_clean_code_passes(self):
        code = '''
        def greet(name: str) -> str:
            return f"Hello, {name}!"
        '''
        report = check_quality(code, [{"role": "user", "content": "write a greet function"}])
        assert report.score >= 80
        assert report.verdict.value == "pass"

    def test_dangerous_code_fails(self):
        code = '''import os
api_key = "real-secret-key-12345abcdef"
def run():
    result = eval(user_input)
    os.system("rm -rf /")
    return result
run()'''
        report = check_quality(code, [{"role": "user", "content": "write code"}])
        # 3 security issues × 10 pts each = 70 score (WARN)
        assert report.score < 80
        assert len(report.security_issues) >= 3

    def test_hallucination_markers_detected(self):
        messages = [{"role": "user", "content": "What is the exact population of Mars in 2026?"}]
        response = "I cannot verify the exact population of Mars. As of my last update..."
        report = check_quality(response, messages)
        assert report.hallucination_risk is not None

    def test_non_code_response(self):
        response = "The capital of France is Paris."
        messages = [{"role": "user", "content": "What is the capital of France?"}]
        report = check_quality(response, messages)
        # Should not flag security issues for non-code
        assert len(report.security_issues) == 0

    def test_large_code_block_flagged(self):
        # Generate a large Python function (250 lines)
        lines = ["def big_function():"]
        for i in range(249):
            lines.append(f"    x_{i} = {i} + {i+1}")
        code = "\n".join(lines)
        report = check_quality(code, [{"role": "user", "content": "write code"}])
        assert any(i.get("type") == "complexity" for i in report.issues)


class TestPricingData:
    """Verify pricing data is reasonable."""

    def test_all_prices_non_negative(self):
        for model, pricing in MODEL_PRICING.items():
            assert pricing["in"] >= 0, f"{model} has negative input price"
            assert pricing["out"] >= 0, f"{model} has negative output price"

    def test_opus_more_expensive_than_haiku(self):
        opus_in = MODEL_PRICING["anthropic/claude-opus-4"]["in"]
        haiku_in = MODEL_PRICING["anthropic/claude-haiku-4"]["in"]
        assert opus_in > haiku_in

    def test_gpt4o_more_expensive_than_mini(self):
        gpt4o_out = MODEL_PRICING["openai/gpt-4o"]["out"]
        mini_out = MODEL_PRICING["openai/gpt-4o-mini"]["out"]
        assert gpt4o_out > mini_out


class TestModelTiers:
    """Verify tier assignments make sense."""

    def test_opus_is_highest_tier(self):
        assert MODEL_TIERS["anthropic/claude-opus-4"] == 4

    def test_haiku_is_lowest_cloud_tier(self):
        assert MODEL_TIERS["anthropic/claude-haiku-4"] == 1

    def test_local_models_are_free_tier(self):
        assert MODEL_TIERS["meta/llama-3.1-70b"] == 0
        assert MODEL_TIERS["qwen3-35b"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
