# Integration Guide — Connect any tool to AI Guardian

One line change. That's it. Guardian is fully OpenAI-compatible, so any tool that supports a custom `base_url` works immediately.

---

## Cursor

Settings → Models → OpenAI → Base URL:
```
http://localhost:9191/v1
```

Or set environment variable:
```bash
export OPENAI_BASE_URL=http://localhost:9191/v1
```

Your API key stays the same. Guardian proxies it to the real provider.

---

## Claude Code (Anthropic CLI)

```bash
export ANTHROPIC_BASE_URL=http://localhost:9191/v1
```

Then run `claude` normally. Every request goes through Guardian.

---

## Continue (VS Code / JetBrains)

In `~/.continue/config.json`:
```json
{
  "models": [{
    "title": "GPT-4o (Guardian)",
    "provider": "openai",
    "model": "gpt-4o",
    "apiBase": "http://localhost:9191/v1"
  }]
}
```

---

## OpenAI Python SDK

```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:9191/v1",
    api_key="sk-your-key-here",  # Your real OpenAI key
)

# Works exactly the same. Guardian intercepts and optimizes.
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Summarize this article"}],
)

# Check Guardian metadata
print(response.choices[0].message.content)
# Savings info in response headers
```

---

## Anthropic Python SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:9191/v1",
    api_key="sk-ant-your-key-here",
)

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

---

## LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:9191/v1",
    api_key="sk-your-key-here",
    model="gpt-4o",
)
```

---

## LiteLLM

```python
import litellm

# Point LiteLLM at Guardian
litellm.api_base = "http://localhost:9191/v1"

response = litellm.completion(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello"}],
)
```

---

## cURL (testing)

```bash
# Check health
curl http://localhost:9191/health

# Make a request through Guardian
curl -X POST http://localhost:9191/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-openai-key" \
  -H "x-guardian-user: me" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Summarize the benefits of AI caching"}],
    "max_tokens": 200
  }'

# Check your savings
curl http://localhost:9191/guardian/savings/me

# Check your budget
curl http://localhost:9191/guardian/budget/me

# Check your subscription
curl http://localhost:9191/guardian/subscription/me

# View dashboard
open http://localhost:9191/dashboard/me
```

---

## Guardian Headers

Every response includes these headers for observability:

| Header | Description |
|--------|-------------|
| `X-Guardian-Request-Id` | Unique request ID |
| `X-Guardian-Time` | Request duration |

And in the response JSON, `guardian` block contains:

| Field | Description |
|-------|-------------|
| `guardian.cost_usd` | Actual cost of this request |
| `guardian.cache_hit` | Whether response came from cache |
| `guardian.savings` | Savings breakdown (routing, cache, tokens) |
| `guardian.task_type` | How Guardian classified the request |
| `guardian.smart_max_tokens` | Token optimization details |
| `guardian.budget_status` | Current budget status |
| `guardian.routed` | Model routing details if rerouted |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GUARDIAN_PORT` | 9191 | Port to run on |
| `GUARDIAN_SECRET` | change-me | Encryption key for stored API keys |
| `DEFAULT_MONTHLY_BUDGET` | 100 | Monthly budget (USD) |
| `DEFAULT_DAILY_BUDGET` | 10 | Daily budget (USD) |
| `DEFAULT_HARD_CAP` | true | Block requests over budget |
| `PREFER_CHEAP_MODELS` | true | Auto-downgrade to cheaper models |
| `STRIPE_SECRET_KEY` | — | Stripe API key for billing |
| `STRIPE_WEBHOOK_SECRET` | — | Stripe webhook signing secret |
| `STRIPE_PRICE_PERSONAL` | — | Stripe price ID for $9/mo |
| `STRIPE_PRICE_TEAM` | — | Stripe price ID for $29/mo |
| `STRIPE_PRICE_SCALE` | — | Stripe price ID for $99/mo |

---

## Deploy with Docker

```bash
docker build -t ai-guardian .
docker run -p 9191:9191 \
  -e GUARDIAN_SECRET=$(openssl rand -hex 32) \
  -e STRIPE_SECRET_KEY=sk_live_... \
  ai-guardian
```

## Deploy on Railway / Render / Fly.io

Just point to the GitHub repo. Set environment variables in the dashboard. Guardian binds to `0.0.0.0:9191` by default.
