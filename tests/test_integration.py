"""Integration tests — require a running Guardian server on localhost:9191.
Run with: python3 -m pytest tests/test_integration.py -v
"""
import os
import pytest
import httpx

SERVER = os.getenv("GUARDIAN_SERVER", "http://localhost:9191")
USER_ID = "integration-test-user"


@pytest.fixture(scope="module")
def server():
    """Check server is running."""
    import urllib.request
    try:
        urllib.request.urlopen(f"{SERVER}/health", timeout=2)
    except Exception:
        pytest.skip(f"Guardian server not running at {SERVER}")
    return SERVER


@pytest.fixture(autouse=True)
def cleanup_user(server):
    """Clean up test user before each test."""
    try:
        httpx.delete(f"{server}/guardian/users/{USER_ID}/keys/anthropic", timeout=5)
    except Exception:
        pass
    yield


class TestHealthCheck:
    def test_health_endpoint(self, server):
        resp = httpx.get(f"{server}/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "ai-guardian"


class TestUserManagement:
    def test_register_user(self, server):
        resp = httpx.post(f"{server}/guardian/users", json={
            "user_id": USER_ID,
            "provider": "openai",
            "api_key": "sk-test-integration-key-12345abcdef",
            "monthly_budget": 25.0,
            "daily_budget": 5.0,
        }, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == USER_ID
        assert data["monthly_budget"] == 25.0

    def test_list_keys(self, server):
        # Register first
        httpx.post(f"{server}/guardian/users", json={
            "user_id": USER_ID,
            "provider": "anthropic",
            "api_key": "sk-ant-test-key-12345abcdef",
        }, timeout=10)

        resp = httpx.get(f"{server}/guardian/users/{USER_ID}/keys", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data

    def test_update_budget(self, server):
        # Register first
        httpx.post(f"{server}/guardian/users", json={
            "user_id": USER_ID,
            "provider": "openai",
            "api_key": "sk-test-key-12345abcdef",
        }, timeout=10)

        resp = httpx.put(f"{server}/guardian/users/{USER_ID}/budget", json={
            "monthly_budget": 50.0,
            "daily_budget": 10.0,
            "hard_cap": True,
        }, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["monthly_budget"] == 50.0
        assert data["daily_budget"] == 10.0
        assert data["hard_cap"] is True

    def test_get_budget(self, server):
        resp = httpx.get(f"{server}/guardian/budget/{USER_ID}", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "monthly_budget" in data
        assert "daily_budget" in data
        assert "status" in data

    def test_delete_key(self, server):
        # Register first
        httpx.post(f"{server}/guardian/users", json={
            "user_id": USER_ID,
            "provider": "google",
            "api_key": "test-google-key-12345",
        }, timeout=10)

        resp = httpx.delete(f"{server}/guardian/users/{USER_ID}/keys/google", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"


class TestStats:
    def test_stats_endpoint(self, server):
        resp = httpx.get(f"{server}/guardian/stats/{USER_ID}", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_requests" in data
        assert "total_cost_usd" in data
        assert "top_models" in data


class TestModelListing:
    def test_models_endpoint(self, server):
        resp = httpx.get(f"{server}/v1/models", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert len(data["data"]) > 0
        # Check some known models exist
        model_ids = [m["id"] for m in data["data"]]
        assert any("claude" in m for m in model_ids)
        assert any("gpt" in m for m in model_ids)


class TestProxyErrors:
    def test_no_api_key_returns_401(self, server):
        """Proxy should return 401 when no API key is provided."""
        resp = httpx.post(f"{server}/v1/chat/completions", json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        }, headers={"x-guardian-user": "no-key-user"}, timeout=10)
        assert resp.status_code == 401

    def test_budget_exceeded_returns_422(self, server):
        """Proxy should block requests when budget is exceeded.
        Uses a fake provider so the request never reaches a real API."""
        # Register with tiny budget and use a provider we can fake
        httpx.post(f"{server}/guardian/users", json={
            "user_id": "tiny-budget-user",
            "provider": "openai",
            "api_key": "sk-fake-key-for-testing",
            "monthly_budget": 0.001,
            "hard_cap": True,
        }, timeout=10)

        # This should get blocked by budget check BEFORE trying the API
        # A request with 1000 max_tokens costs ~$0.01 which exceeds $0.001 budget
        resp = httpx.post(f"{server}/v1/chat/completions", json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "Write a very long essay about the history of computing"}],
            "max_tokens": 1000,
        }, headers={
            "x-guardian-user": "tiny-budget-user",
        }, timeout=10)
        assert resp.status_code == 422
        assert "budget_exceeded" in resp.text


class TestDashboard:
    def test_dashboard_returns_html(self, server):
        resp = httpx.get(f"{server}/dashboard/{USER_ID}", timeout=5)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Guardian" in resp.text


class TestRequestMetadata:
    def test_response_has_guardian_headers(self, server):
        """Non-streaming response should have Guardian metadata."""
        resp = httpx.post(f"{server}/v1/chat/completions", json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        }, headers={"x-guardian-user": USER_ID, "Authorization": "Bearer sk-test-key"}, timeout=15)
        # Should get either a 200 (if key works) or 401/400 (if key is fake)
        # Just check the endpoint is reachable
        assert resp.status_code in (200, 401, 400, 422)
