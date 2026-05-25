# AI Guardian 🛡️

**The smart firewall for AI.** Sits between your tools (Claude Code, Cursor, custom apps) and AI providers (OpenAI, Anthropic, etc.) to control costs, enforce quality, and monitor agents.

## The Problem

- Claude Code cache bugs silently **10-20x your API costs**
- Companies like **Uber burned their entire AI coding budget in 4 months**
- AI-generated code has **118 functions running 446x slower** than necessary
- Developers are **burning $6K overnight** from runaway agents
- **Zero tools** combine cost control + quality guardrails + agent monitoring

## What Guardian Does

### Layer 1: Cost Guardrails
- Real-time spending dashboards per user/project/agent
- Hard budget caps with automatic cutoffs (no surprise bills)
- Smart model routing — uses the cheapest model that can handle the task
- Prompt caching optimization
- Token usage analytics

### Layer 2: Quality Guardrails
- Code syntax validation
- Security scanning (hardcoded keys, SQL injection, eval/exec, etc.)
- Performance anti-pattern detection (nested loops, N+1 queries, etc.)
- Complexity analysis
- Hallucination risk detection

### Layer 3: Agent Monitoring
- Track agent sessions with iteration limits
- Timeout protection for runaway agents
- Per-agent cost allocation
- Session persistence

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp .env.example .env

# Run the server
python main.py
```

The proxy runs on port 9191 by default.

## Usage

Point any OpenAI-compatible client at Guardian:

```bash
curl http://localhost:9191/v1/chat/completions \
  -H "Authorization: Bearer YOUR_OPENAI_KEY" \
  -H "x-guardian-user: your-user-id" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Headers for Guardian features:
- `x-guardian-user` — User ID for budget tracking
- `x-guardian-project` — Project ID for cost allocation
- `x-guardian-agent` — Agent ID for session monitoring
- `x-guardian-session` — Session ID for iteration tracking

### Endpoints:
- `POST /v1/chat/completions` — Main proxy (OpenAI-compatible)
- `GET /v1/models` — List available models
- `GET /health` — Health check
- `GET /guardian/stats/{user_id}` — JSON stats
- `GET /guardian/budget/{user_id}` — Budget status
- `GET /dashboard/{user_id}` — HTML dashboard

## Supported Providers

- OpenAI (gpt-4o, gpt-4o-mini, o1, o3-mini, etc.)
- Anthropic (claude-opus-4, claude-sonnet-4, claude-haiku-4)
- Google (gemini-2.5-pro, gemini-2.5-flash)
- DeepSeek (deepseek-v3, deepseek-r1)
- Any OpenAI-compatible API (Ollama, LM Studio, etc.)

## License

MIT
