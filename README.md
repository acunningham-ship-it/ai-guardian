# AI Guardian 🛡️

**The smart firewall for AI.** Control costs, enforce quality, and monitor agents — all through one proxy.

## The Problem

- 🔥 Uber burned its entire 2026 AI coding budget in 4 months ($500-2k/engineer/month)
- 🐛 Claude Code cache bugs silently **10-20x your API costs**
- 💸 Developers accidentally burn **$6,000 overnight** from runaway agents
- 🐌 AI-generated code has functions running **446x slower** than necessary
- ❌ **Zero tools** combine cost control + quality guardrails + agent monitoring

## What Guardian Does

### Layer 1: Cost Guardrails
- Set monthly/daily budgets per user/project
- **Auto-downgrade** expensive models based on task complexity (Opus→Sonnet→Haiku)
- Hard caps when budget is exceeded — requests blocked **before** reaching the API
- Real-time spending dashboards

### Layer 2: Quality Guardrails
- **Security scanning**: Hardcoded keys, SQL injection, eval/exec, pickle, shell injection
- **Performance checks**: Nested loops, N+1 queries, blocking calls
- **Code complexity**: Large functions, deep nesting
- **Hallucination detection**: Uncertainty markers, generic responses

### Layer 3: Agent Monitoring
- Track autonomous agent sessions
- Iteration caps and timeout protection
- Per-agent cost allocation

## Quick Start

```bash
git clone https://github.com/**it/ai-guardian
cd ai-guardian
pip install -r requirements.txt
python3 main.py &

# Register your API key (encrypted storage)
curl -X POST localhost:9191/guardian/users \
  -H "Content-Type: application/json" \
  -d '{"user_id":"me","provider":"anthropic","api_key":"sk-ant-...","monthly_budget":50}'

# Point any tool at Guardian
export OPENAI_BASE_URL=http://localhost:9191/v1        # Cursor, etc.
export ANTHROPIC_BASE_URL=http://localhost:9191/v1     # Claude Code

# Check your dashboard
open http://localhost:9191/dashboard/me
```

## Docker

```bash
docker build -t ai-guardian .
docker run -p 9191:9191 -e GUARDIAN_SECRET=$(openssl rand -hex 32) ai-guardian
```

## CLI

```bash
python3 cli.py register me --provider anthropic --api-key sk-ant-... --monthly-budget 50
python3 cli.py budget me
python3 cli.py stats me
python3 cli.py test me --message "Hello world"
python3 cli.py serve
```

## API

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Main proxy (OpenAI-compatible, streaming supported) |
| `POST /guardian/users` | Register user with encrypted API key |
| `PUT /guardian/users/{id}/budget` | Update budget |
| `GET /guardian/budget/{id}` | Budget status |
| `GET /guardian/stats/{id}` | Usage statistics |
| `GET /dashboard/{id}` | HTML dashboard |
| `GET /v1/models` | List available models |
| `GET /health` | Health check |

## Supported Providers

OpenAI · Anthropic · Google · DeepSeek · Any OpenAI-compatible API (Ollama, LM Studio, etc.)

## Model Routing

Guardian automatically picks the cheapest model that can handle your task:

| Task Type | Example | Routed To |
|-----------|---------|-----------|
| Simple | "Hello", summarize | Haiku / GPT-4o-mini |
| Medium | Write a function | Sonnet / GPT-4o |
| Complex | Architecture, debugging | Opus |
| Budget tight | Any | Downgrades 1 tier |

## Tests

```bash
python3 -m pytest tests/ -v
# 39 tests passing (27 unit + 12 integration)
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GUARDIAN_PORT` | 9191 | Server port |
| `GUARDIAN_SECRET` | change-me | Encryption key |
| `DEFAULT_MONTHLY_BUDGET` | 100 | Monthly budget (USD) |
| `DEFAULT_DAILY_BUDGET` | 10 | Daily budget (USD) |
| `DEFAULT_HARD_CAP` | true | Block when budget exceeded |
| `PREFER_CHEAP_MODELS` | true | Auto-downgrade models |
| `ENABLE_CODE_VALIDATION` | true | Quality checks |
| `ENABLE_SECURITY_SCAN` | true | Security scanning |
| `MAX_AGENT_ITERATIONS` | 20 | Agent iteration cap |
| `AGENT_TIMEOUT_SECONDS` | 300 | Agent timeout |

## License

MIT
