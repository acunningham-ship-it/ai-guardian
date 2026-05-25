# AI Guardian — A Smart Proxy That Controls Your AI Costs and Quality

**Problem:** AI coding tools (Claude Code, Cursor) are burning through budgets. Fast.

- Uber burned its entire 2026 AI coding budget in 4 months ($500-2k/engineer/month)
- Claude Code has cache bugs that silently 10-20x your API costs
- Developers accidentally burn $6,000 overnight from runaway agents
- AI-generated code has functions running 406x slower than necessary

**Existing tools** (LangSmith, Helicone, Portkey) do cost tracking OR quality checks. None combine both with agent monitoring.

## What I Built

AI Gateway + Quality Guardian — a reverse proxy that sits between your tools and AI APIs.

**3 layers of protection:**

1. **Cost Guardrails** — Set monthly/daily budgets. Auto-downgrade expensive models (Opus→Sonnet→Haiku) based on task complexity. Hard caps when budget is exceeded.

2. **Quality Guardrails** — Security scanning (hardcoded keys, SQL injection, eval, pickle), performance anti-pattern detection (nested loops, N+1 queries), code complexity analysis, hallucination risk detection.

3. **Agent Monitoring** — Track autonomous agent sessions, iteration caps, timeout protection, per-agent cost allocation.

## Quick Start

```bash
git clone https://github.com/**it/ai-guardian
cd ai-guardian
pip install -r requirements.txt
python3 main.py &

# Register your API key
curl -X POST localhost:9191/guardian/users \
  -H "Content-Type: application/json" \
  -d '{"user_id":"me","provider":"anthropic","api_key":"sk-ant-...","monthly_budget":50}'

# Point Claude Code at it
export ANTHROPIC_BASE_URL=http://localhost:9191/v1

# Check your dashboard
open http://localhost:9191/dashboard/me
```

## What Makes It Different

- **Model routing**: Automatically picks the cheapest model for the task. Simple chat? Uses Haiku. Complex architecture? Uses Opus. No wasted spend.
- **Quality checks**: Catches security holes, performance issues, and suspicious code BEFORE it ships.
- **Agent monitoring**: Autonomous agents can't runaway and burn your budget.
- **One proxy, multiple providers**: OpenAI, Anthropic, Google, DeepSeek — all through one endpoint.

## Tech Stack

Python, FastAPI, SQLAlchemy (async), httpx. ~3,000 lines. 39 tests passing.

## Try It

```bash
# Docker
docker build -t ai-guardian .
docker run -p 9191:9191 ai-guardian

# Or systemd
cp systemd/ai-guardian.service /etc/systemd/system/
systemctl enable --now ai-guardian
```

## What's Next

- Webhook alerts (Slack/Discord for budget thresholds)
- Per-project cost allocation
- Team management with role-based access
- Performance benchmarking (catch 446x slower code)

---

I built this because I kept seeing developers get blindsided by AI costs. The tools exist to track spending, but none actually *prevent* overspending or catch bad code.

Would love feedback on:
- Is the model routing heuristic useful or too aggressive?
- What quality checks matter most to you?
- Would you pay for this? What pricing makes sense?

Repo: https://github.com/**it/ai-guardian
