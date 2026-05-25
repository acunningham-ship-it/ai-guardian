Show HN: AI Guardian — a proxy that controls your AI costs and code quality

I kept seeing developers get blindsided by AI costs. Uber burned their entire 2026 AI coding budget in 4 months. Claude Code has cache bugs that silently 10-20x your API costs. People accidentally burn $6K overnight from runaway agents.

Existing tools (LangSmith, Helicone, Portkey) do cost tracking OR quality checks. None combine both with agent monitoring.

So I built AI Guardian — a reverse proxy that sits between your AI tools (Claude Code, Cursor, etc.) and the AI APIs. Three layers:

1. Cost Guardrails — Set monthly/daily budgets. Auto-downgrade expensive models based on task complexity. Hard caps actually block requests before they hit the API.

2. Quality Guardrails — Security scanning (hardcoded keys, SQL injection, eval, pickle), performance anti-pattern detection (nested loops, N+1 queries), code complexity analysis, hallucination risk detection.

3. Agent Monitoring — Track autonomous agent sessions, iteration caps, timeout protection, per-agent cost allocation.

It's OpenAI-compatible, so any tool that supports a custom base_url works. Point Cursor or Claude Code at Guardian instead of directly at the API.

Tech: Python, FastAPI, SQLAlchemy async, ~3K lines, 39 tests.

GitHub: https://github.com/acunningham-ship-it/ai-guardian

Docker: docker run -p 9191:9191 ai-guardian

CLI: python3 cli.py register me --provider anthropic --api-key sk-ant-... --monthly-budget 50

I'd love feedback on:
- Is the model routing heuristic useful or too aggressive?
- What quality checks matter most?
- Would you use this? What's missing?
