# Getting Started with AI Guardian

## What is AI Guardian?

AI Guardian is a smart proxy that sits between your AI tools (Claude Code, Cursor, etc.) and AI providers (OpenAI, Anthropic, etc.). It gives you:

- **Cost control** — Set budgets, get alerts, auto-downgrade expensive models
- **Quality guardrails** — Security scanning, performance checks, hallucination detection
- **Agent monitoring** — Track autonomous agents, prevent runaway costs

## Quick Start (5 minutes)

### 1. Start the server

```bash
# From the ai-guardian directory
pip install -r requirements.txt
python3 main.py
```

The server starts on `http://localhost:9191`.

### 2. Register your API key

```bash
curl -X POST http://localhost:9191/guardian/users \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "my-team",
    "provider": "anthropic",
    "api_key": "sk-ant-...",
    "monthly_budget": 50,
    "daily_budget": 5
  }'
```

### 3. Point your tools at Guardian

**For Cursor:** Set `base_url` to `http://localhost:9191/v1`

**For Claude Code:** Set environment variable:
```bash
export ANTHROPIC_BASE_URL=http://localhost:9191/v1
```

**For any OpenAI-compatible tool:**
```bash
export OPENAI_BASE_URL=http://localhost:9191/v1
export OPENAI_API_KEY=anything
```

### 4. Send a test request

```bash
curl http://localhost:9191/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-guardian-user: my-team" \
  -d '{
    "model": "anthropic/claude-sonnet-4",
    "messages": [{"role": "user", "content": "Write a Python function to calculate fibonacci"}]
  }'
```

### 5. Check your dashboard

Open `http://localhost:9191/dashboard/my-team` in your browser.

## Using the CLI

```bash
# Register a user
python3 cli.py register my-team --provider anthropic --api-key sk-ant-... --monthly-budget 50

# Check budget
python3 cli.py budget my-team

# View stats
python3 cli.py stats my-team

# Send a test request
python3 cli.py test my-team --message "Hello world"

# Start the server
python3 cli.py serve --port 9191
```

## Setting Up Alerts

Pass a webhook URL with your requests:

```bash
curl http://localhost:9191/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-guardian-user: my-team" \
  -H "x-guardian-webhook: https://hooks.slack.com/services/..." \
  -d '{"model": "anthropic/claude-sonnet-4", "messages": [...]}'
```

You'll get notified when:
- Budget hits 80% (warning)
- Budget is exceeded (critical)
- AI generates low-quality or insecure code
- An agent runs too many iterations

## Docker Deployment

```bash
docker build -t ai-guardian .
docker run -d \
  --name guardian \
  -p 9191:9191 \
  -e GUARDIAN_SECRET=my-secret-key \
  -e DEFAULT_MONTHLY_BUDGET=100 \
  ai-guardian
```

## Configuration

Environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `GUARDIAN_PORT` | 9191 | Server port |
| `GUARDIAN_SECRET` | change-me | Encryption key for stored API keys |
| `DEFAULT_MONTHLY_BUDGET` | 100 | Default monthly budget per user (USD) |
| `DEFAULT_DAILY_BUDGET` | 10 | Default daily budget per user (USD) |
| `DEFAULT_HARD_CAP` | true | Block requests when budget exceeded |
| `PREFER_CHEAP_MODELS` | true | Auto-downgrade to cheaper models |
| `ENABLE_CODE_VALIDATION` | true | Run quality checks on code |
| `ENABLE_SECURITY_SCAN` | true | Scan for security issues |
| `MAX_AGENT_ITERATIONS` | 20 | Max iterations per agent session |
| `AGENT_TIMEOUT_SECONDS` | 300 | Agent session timeout |

## How Model Routing Works

Guardian automatically picks the cheapest model that can handle your task:

1. **Simple tasks** (hello, summarize, translate) → Haiku / GPT-4o-mini
2. **Medium tasks** (write a function, explain code) → Sonnet / GPT-4o
3. **Complex tasks** (architecture, debugging, security) → Opus / GPT-4o
4. **Budget tight?** → Downgrades one tier to save money

You always get the right tool for the job — without overpaying.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | Main proxy (OpenAI-compatible) |
| `/v1/models` | GET | List available models |
| `/health` | GET | Health check |
| `/guardian/users` | POST | Register a user |
| `/guardian/users/{id}/keys` | GET | List stored key providers |
| `/guardian/users/{id}/keys/{provider}` | DELETE | Delete a stored key |
| `/guardian/users/{id}/budget` | PUT | Update budget |
| `/guardian/budget/{id}` | GET | Get budget status |
| `/guardian/stats/{id}` | GET | Get usage statistics |
| `/dashboard/{id}` | GET | HTML dashboard |
